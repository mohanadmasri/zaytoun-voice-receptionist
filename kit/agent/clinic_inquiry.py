"""Read-only clinic questions that can interrupt a booking turn."""

from datetime import date, timedelta
import re

from agent.dates import ARABIC_WEEKDAYS, explicit_calendar_date_text, normalize_date, relative_date_text
from agent.guardrails import check_guardrails, normalize_text
from agent.presentation import display_time, say
from agent.scheduler import extract_slots, filter_matching_slots, find_available_slots
from agent.slot_handoff import appointment_has_passed, build_find_slots_args, clinic_local_now, scheduling_signature, slot_matches_offer
from agent.slot_selection import format_slot_options, offered_clock_times

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ARABIC_DAYS = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
DAY_WORDS = {**{normalize_text(word): WEEKDAYS[index] for word, index in ARABIC_WEEKDAYS.items()},
             **{day: day for day in WEEKDAYS}}
DAY_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(DAY_WORDS) + r")(?!\w)")
HOURS = re.compile(
    r"opening hours|closing hours|working (?:hours|days)|business hours"
    r"|\bwhen (?:do you|does (?:the )?clinic) (?:open|close)\b"
    r"|\bwhat time (?:do you|does (?:the )?clinic) (?:open|close)\b"
    r"|\b(?:are you|is (?:the )?clinic) (?:open|closed)\b"
    r"|(?:ساعات|مواعيد|ايام|أيام)\s+(?:فتح|استقبال|الاستقبال|عمل|العمل|دوام|الدوام|اغلاق|إغلاق)"
    r"|متى\s+(?:بتفتحوا|بتفتح|بتسكروا|بتسكر|بيفتح|بيسكر|تفتح|تغلق)"
    r"|(?:اي|أي) ساعة\s+(?:فاتحين|بتفتحوا|بتسكروا)"
    r"|(?:هل\s+)?العيادة\s+(?:مفتوحة|مسكرة|مغلقة)|(?:مفتوح(?:ة)?|مسكر(?:ة)?)\s+يوم"
)


def inquiry_language(state):
    message = state.get("current_message", "")
    return state.get("current_input_language") or ("ar" if re.search(r"[\u0600-\u06ff]", message) else "en")


EARLIEST = re.compile(
    r"(?:(?:شو|بدي)\s+)?(?:[أا]قرب (?:موعد|يوم)(?: موجود| متاح)?"
    r"|[أا]ول موعد (?:فاضي|متوفر)|[أا]سرع موعد(?: عندكم)?)"
    r"|(?:(?:what(?:'s| is)|i (?:want|need))\s+)?(?:the )?"
    r"(?:earliest (?:available )?appointment|first available (?:slot|appointment)|soonest appointment)"
)


def inquiry_kind(state):
    text = normalize_text(state.get("current_message", ""))
    if state.get("service_id") and EARLIEST.fullmatch(text.strip(" .!?؟،")):
        kind = "earliest"
    else:
        kind = "hours" if HOURS.search(text) else availability_kind(text)
    if kind and check_guardrails(text, inquiry_language(state))["kind"] == "allow":
        return kind
    return "intake"


AVAILABILITY = re.compile(
    r"(?<!\w)(?:هل\s+)?(?:في|عندكم)\s+موعد(?!\w)|(?:فاضية|متاحة)(?!\w)"
    r"|\b(?:is|are)\s+.+?\s+(?:available|free|open)\b"
    r"|\b(?:do you have|is there)\s+(?:an?\s+)?(?:appointment|slot)\b"
)


def availability_kind(text):
    if AVAILABILITY.search(text) and re.search(r"الساعة|الساعه|\d|\b(?:am|pm|o'clock)\b", text):
        return "availability"
    return None


def question_clocks(text):
    clocks = offered_clock_times(text)
    if clocks != []:
        return clocks or []
    # A repeated clock can supply a period on one side of the question cue.
    cue = AVAILABILITY.search(text)
    if cue:
        left = offered_clock_times(text[:cue.start()])
        right = offered_clock_times(text[cue.end():])
        if left and right:
            return [clock for clock in left if clock in right]
    return []


def parse_hours(raw):
    if not isinstance(raw, dict):
        return {}
    return {
        day: window for day in WEEKDAYS if day in raw
        and ((window := raw[day]) is None or (
            isinstance(window, dict)
            and all(isinstance(window.get(key), str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", window[key])
                    for key in ("open", "close"))
            and window["open"] < window["close"]
        ))
    }


def day_name(day, language):
    return ARABIC_DAYS[WEEKDAYS.index(day)] if language == "ar" else day.capitalize()


def hours_label(window, language):
    start, end = (display_time(window[key], language) for key in ("open", "close"))
    return f"من {start} إلى {end}" if language == "ar" else f"{start} to {end}"


def hours_reply(text, hours, language):
    day_match = DAY_PATTERN.search(text)
    days = [DAY_WORDS[day_match[0]]] if day_match else list(hours)
    lines = []
    for day in days:
        if day in hours:
            window = hours[day]
            label = hours_label(window, language) if window else ("مغلقة" if language == "ar" else "closed")
            lines.append(f"{day_name(day, language)}: {label}")
    if lines:
        return ("ساعات دوام العيادة:\n" if language == "ar" else "Clinic hours:\n") + "\n".join(lines)
    return say({"language": language}, "I couldn't verify the clinic hours for that day.",
               "ما قدرت أتحقق من ساعات دوام العيادة لهاليوم.")


def inside(window, clock):
    return window is not None and window["open"] <= clock < window["close"]


async def availability_reply(text, state, clinic, hours, gateway):
    language = state["language"]
    def reply(en, ar):
        return say(state, en, ar)

    if not hours:
        return reply("I couldn't verify appointment availability. Please try again.",
                     "ما قدرت أتحقق من المواعيد. جرّب مرة تانية.")
    now = clinic_local_now(clinic)
    today = now.date().isoformat() if now else clinic.get("today")
    date_text = explicit_calendar_date_text(text) or relative_date_text(text)
    weekday = DAY_PATTERN.search(text)
    if date_text is None and weekday:
        date_text = weekday[0]
    day = normalize_date(date_text, today, allow_past=True) if date_text and today else state.get("requested_date")
    if not day:
        return reply("Which day should I check?", "أي يوم بدك أتحقق منه؟")
    weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
    label = day_name(weekday, language)
    calendar = {**state, "clinic_today": today, "clinic_calendar": clinic}
    if appointment_has_passed(calendar, day):
        return reply("That date has passed. Which upcoming day works for you?", "هالتاريخ مرق. أي يوم جاي بناسبك؟")
    if weekday not in hours:
        return reply("I couldn't verify the clinic hours for that day.", "ما قدرت أتحقق من ساعات دوام العيادة لهاليوم.")
    window = hours[weekday]
    clocks = question_clocks(text)
    if not clocks:
        return reply("Please repeat the exact hour and minute.", "ممكن تعيد الساعة والدقيقة بالضبط؟")
    preferred = state.get("requested_time") if day == state.get("requested_date") else None
    if not window:
        return reply(f"The clinic is closed on {label}.", f"العيادة مسكّرة يوم {label}.")
    possible = [clock for clock in clocks if inside(window, clock)]
    if preferred in clocks:
        clock = preferred
    elif len(possible) == 1:
        clock = possible[0]
    elif len(clocks) == 1:
        clock = clocks[0]
    else:
        clock = None
    if (clock is not None and not inside(window, clock)) or not possible:
        period = hours_label(window, language)
        time = display_time(clock, language) if clock else None
        return reply(
            f"{time or 'That time'} is outside clinic opening hours. {label}: {period}.",
            f"الساعة {time} خارج دوام العيادة. {label}: {period}." if time else f"هالساعة خارج دوام العيادة. {label}: {period}.",
        )
    if clock is None:
        return reply("Did you mean morning or afternoon?", "قصدك صباحًا ولا مساءً؟")
    if appointment_has_passed(calendar, day, clock):
        return reply("That time has passed. Which later time works for you?", "هالساعة مرقت. أي ساعة بعدها بتناسبك؟")
    if not state.get("service_id"):
        return reply("Which service should I check?", "أي خدمة بدك أتحقق منها؟")

    search = {**calendar, "stage": "needs_slot_lookup", "requested_date": day,
              "time_kind": "exact", "requested_time": clock}
    result = await find_available_slots(gateway, build_find_slots_args(search, limit=5))
    time = display_time(clock, language)
    if filter_matching_slots(search, extract_slots(result)):
        return reply(f"{time} is available on {label}.", f"الساعة {time} متاحة يوم {label}.")
    unavailable = reply(f"{time} is unavailable on {label}.", f"الساعة {time} مش متاحة يوم {label}.")
    if not any(slot_matches_offer(calendar, slot) for slot in state.get("proposed_slots", [])):
        search.update(time_kind="missing", requested_time=None)
        result = await find_available_slots(gateway, build_find_slots_args(search, limit=5))
        slots = filter_matching_slots(search, extract_slots(result))
        times = list(dict.fromkeys(display_time(slot["start"], language) for slot in slots
                                  if inside(window, slot["start"])))
        if times:
            unavailable += reply(" Other times available: " + ", ".join(times) + ".",
                                 " في مواعيد تانية: " + "، ".join(times) + ".")
    return unavailable


async def earliest_offer(state, clinic, hours, gateway):
    now = clinic_local_now(clinic)
    if now is None:
        raise ValueError("Clinic clock unavailable")
    today = now.date()
    last = min(today + timedelta(days=13), date.fromisoformat(
        clinic.get("last_bookable_day") or (today + timedelta(days=13)).isoformat()))
    search = {**state, "stage": "needs_slot_lookup", "requested_date": today.isoformat(),
              "clinic_today": today.isoformat(), "clinic_calendar": clinic,
              "time_kind": "missing", "requested_time": None, "time_after": None, "time_before": None}
    slots = []
    if last >= today:
        arguments = build_find_slots_args(search, limit=50)
        arguments["date_to"] = last.isoformat()
        result = await find_available_slots(gateway, arguments)
        for slot in extract_slots(result):
            if not isinstance(slot, dict):
                continue
            try:
                day = date.fromisoformat(slot.get("date", ""))
            except (ValueError, TypeError):
                continue
            if not today <= day <= last:
                continue
            candidate = {**search, "requested_date": day.isoformat()}
            weekday = WEEKDAYS[day.weekday()]
            if filter_matching_slots(candidate, [slot]) and (
                weekday not in hours or inside(hours[weekday], slot["start"])
            ):
                slots.append(slot)
    if not slots:
        return {"reply": say(state, "No appointments are available in the search window.",
                             "ما في مواعيد متاحة بالفترة اللي تحققنا منها.")}

    slots.sort(key=lambda slot: (slot["date"], slot["start"], str(slot["slot_id"])))
    day = slots[0]["date"]
    slots = [slot for slot in slots if slot["date"] == day][:5]
    search["requested_date"] = day
    signature = scheduling_signature(search)
    return {
        **signature, "date_text": day, "time_text": None, "time_outside_hours": False,
        "clinic_today": today.isoformat(), "clinic_calendar": clinic,
        "date_has_passed": False, "date_conflict": None, "date_clarification_context": None,
        "time_change_pending": False, "time_change_clocks": [], "selection_candidates": [],
        "stage": "slots_proposed", "proposed_slots": slots, "pending_action": None, "selected_slot_id": None,
        "offer_context": {"request": signature, "search": signature, "slot_ids": [slot["slot_id"] for slot in slots]},
        "reply": format_slot_options(slots, state),
    }


async def clinic_inquiry(state, runtime):
    language = inquiry_language(state)
    view = {**state, "language": language}
    message = state.get("current_message", "")
    text = normalize_text(message)
    try:
        clinic = await runtime.context["gateway"].call_read("clinic_info", {})
        hours = parse_hours(clinic.get("opening_hours")) if clinic.get("ok") is True else {}
    except Exception:
        clinic, hours = {}, {}
    if inquiry_kind(state) == "earliest":
        try:
            updates = await earliest_offer(view, clinic, hours, runtime.context["gateway"])
        except Exception:
            updates = {"reply": say(view, "I couldn't check the earliest appointment. Please try again.",
                                    "ما قدرت أتحقق من أقرب موعد. ممكن إعادة المحاولة؟")}
        reply = updates["reply"]
        return {**updates, "inquiry_reply": None if updates.get("proposed_slots") else reply,
                "language": language, "messages": [*state.get("messages", []), {"role": "user", "content": message}]}
    if HOURS.search(text):
        reply = hours_reply(text, hours, language)
    else:
        try:
            reply = await availability_reply(text, view, clinic, hours, runtime.context["gateway"])
        except Exception:
            reply = say(view, "I couldn't verify appointment availability. Please try again.",
                        "ما قدرت أتحقق من المواعيد. جرّب مرة تانية.")
    calendar = {**view, "clinic_today": clinic.get("today"), "clinic_calendar": clinic}
    if not state.get("selected_slot_id") and any(
        slot_matches_offer(calendar, slot) for slot in state.get("proposed_slots", [])
    ):
        reply += say(view, "\nYou can continue choosing from the appointments already shown.",
                     "\nخيارات المواعيد المعروضة بعدها متاحة للاختيار.")
    return {"reply": reply, "inquiry_reply": reply, "language": language,
            "messages": [*state.get("messages", []), {"role": "user", "content": message}]}
