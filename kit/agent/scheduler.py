
# scheduler.py
# هذا الملف مسؤول عن البحث عن المواعيد الحقيقية في العيادة.
# يستخدم Slot Handoff لتجهيز شروط البحث والتحقق من النتائج.
# يدعم الطبيب المحدد والطبيب المفضل والبحث عن البدائل.
# لا ينشئ أي حجز ولا يلغي أي موعد.
# المدخل AgentState وبوابة قراءة؛ المخرج مواعيد مصفاة ورد يعرضها بنفس الترتيب.

from agent.state import AgentState
from agent.slot_handoff import (
    appointment_has_passed,
    build_find_slots_args,
    slot_matches_request,
    scheduling_signature,
)
from core.contracts import ClinicGateway
from agent.slot_selection import format_slot_options
from agent.presentation import display_time, say
from datetime import date, timedelta
from agent.times import clocks_within_clinic_hours


# إرسال طلب البحث الحقيقي إلى العيادة عبر MCP.
async def find_available_slots(
    gateway: ClinicGateway,
    arguments: dict,
) -> dict:

    # استبعاد الحقول الفارغة من طلب البحث.
    clean_arguments = {
        key: value
        for key, value in arguments.items()
        if value is not None
    }

    # استخدام أداة قراءة فقط دون تنفيذ أي حجز.
    return await gateway.call_read(
        "find_slots",
        clean_arguments,
    )


# التأكد من صحة استجابة العيادة.
def extract_slots(result: dict) -> list[dict]:

    # يجب أن تكون الاستجابة ناجحة.
    if not isinstance(result, dict):
        raise ValueError("Invalid clinic response")

    if result.get("ok") is not True:
        raise ValueError("Clinic slot search failed")

    # يجب أن تحتوي الاستجابة على قائمة مواعيد.
    slots = result.get("slots")

    if not isinstance(slots, list):
        raise ValueError("Invalid slots list")

    return slots


# الاحتفاظ فقط بالمواعيد التي تطابق طلب المريض.
def filter_matching_slots(
    state: AgentState,
    slots: list[dict],
) -> list[dict]:

    # نستخدم دالة التحقق التي جهزها Developer A.
    return [
        slot
        for slot in slots
        if slot_matches_request(state, slot)
    ]


# البحث عن المواعيد بعد اكتمال معلومات Intake.
async def schedule(
    state: AgentState,
    gateway: ClinicGateway,
) -> dict:

    # قراءة طريقة اختيار الطبيب.
    provider_choice = state.get(
        "provider_choice",
        "auto",
    )

    # تحديد الطبيب الحالي والطبيب المفضل.
    provider_id = state.get("provider_id")
    preferred_provider_id = state.get(
        "preferred_provider_id"
    )

    # لا نبدأ البحث قبل أن تصبح معلومات Intake جاهزة.
    if state.get("stage") != "needs_slot_lookup":
        return {
            "stage": "awaiting_details",
            "reply": say(state, "Please complete the appointment details first.", "خلينا نكمّل تفاصيل الموعد أولًا."),
            "proposed_slots": [],
        }

    # لا نسمح بتجاهل الطبيب المحدد صراحة.
    if provider_choice == "specific" and not provider_id:
        return {
            "stage": "awaiting_provider_clarification",
            "reply": say(state, "Which doctor would you prefer?", "عند أي طبيب بتفضّل؟"),
            "proposed_slots": [],
        }

    # تجهيز نسخة من الحالة للبحث الأول.
    search_state = dict(state)

    # اختيار الطبيب المناسب للبحث الأول.
    if provider_choice == "any":
        search_state["provider_id"] = None

    elif provider_choice == "specific":
        search_state["provider_id"] = provider_id

    else:
        search_state["provider_id"] = (
            provider_id or preferred_provider_id
        )

    # تجهيز معاملات MCP بالاعتماد على Slot Handoff.
    day = state.get("requested_date")
    if day and appointment_has_passed(state, day):
        return {
            "stage": "awaiting_details",
            "requested_date": None,
            "date_text": None,
            "proposed_slots": [],
            "reply": say(state, "That date has passed. Which upcoming day works for you?",
                         "هالتاريخ مرق. أي يوم جاي بناسبك؟"),
        }
    cutoff = (state.get("requested_time") if state.get("time_kind") == "exact"
              else state.get("time_before"))
    if day and cutoff and appointment_has_passed(state, day, cutoff):
        return {
            "stage": "awaiting_time_clarification",
            "proposed_slots": [],
            "reply": say(state, "That time has passed. Which later time works for you?",
                         "هالساعة مرقت. أي ساعة بعدها بتناسبك؟"),
        }
    calendar = state.get("clinic_calendar") or {}
    hours = calendar.get("opening_hours") or {}
    try:
        requested_day = date.fromisoformat(day) if day else None
    except (ValueError, TypeError):
        requested_day = None
    weekday = requested_day.strftime("%A").lower() if requested_day else None
    closed = weekday in hours and not hours[weekday]
    outside_hours = state.get("time_outside_hours") and state.get("time_kind") == "exact"
    if closed or outside_hours:
        search_state.update(time_kind="missing", requested_time=None, time_after=None, time_before=None)
        if provider_choice == "auto":
            search_state["provider_id"] = None
    if closed:
        search_state["requested_date"] = (requested_day + timedelta(days=1)).isoformat()
        last_day = calendar.get("last_bookable_day") or (requested_day + timedelta(days=3)).isoformat()
        if search_state["requested_date"] > last_day:
            return {"stage": "no_slots", "proposed_slots": [], "reply": say(
                state, "The clinic is closed on your requested day and no later appointments are available.",
                "العيادة مسكّرة باليوم المطلوب وما في مواعيد متاحة بعده.")}
    try:
        arguments = build_find_slots_args(
            search_state,
            limit=5,
        )

        if closed:
            arguments["date_to"] = last_day

    # التعامل مع بيانات ناقصة أو غير صالحة.
    except (ValueError, TypeError) as error:

        # عدم تخمين الوقت إذا كان غامضا.
        if state.get("time_kind") in (
            "ambiguous",
            "unrecognized",
        ):
            if state.get("time_kind") == "unrecognized":
                reply = say(state, "Please repeat the exact hour and minute.", "ممكن تعيد الساعة والدقيقة بالضبط؟")
            else:
                reply = say(state, "What time would you prefer: morning or afternoon?", "أي ساعة بتناسبك، صباحًا ولا مساءً؟")
            return {
                "stage": "awaiting_time_clarification",
                "reply": reply,
                "proposed_slots": [],
            }

        return {
            "stage": "awaiting_details",
            "reply": say(state, "Could you clarify the appointment details?", "ممكن توضّح تفاصيل الموعد؟"),
            "proposed_slots": [],
        }

    # حفظ حالة البحث عن أطباء بديلين.
    used_alternatives = False
    used_time_alternatives = False
    used_date_alternatives = False

    try:
        # البحث الأول باستخدام شروط المريض.
        result = await find_available_slots(
            gateway,
            arguments,
        )

        # استخراج المواعيد والتحقق من مطابقتها.
        slots = extract_slots(result)

        if closed:
            for slot_day in sorted({slot["date"] for slot in slots if isinstance(slot, dict) and isinstance(slot.get("date"), str)
                                    and arguments["date_from"] <= slot["date"] <= last_day}):
                candidate = {**search_state, "requested_date": slot_day}
                matching_slots = [slot for slot in filter_matching_slots(candidate, slots)
                                  if clocks_within_clinic_hours(candidate, [slot["start"]]) != []]
                if matching_slots:
                    search_state = candidate
                    used_date_alternatives = True
                    break
            else:
                matching_slots = []
        else:
            matching_slots = filter_matching_slots(search_state, slots)
            if outside_hours:
                matching_slots = [slot for slot in matching_slots
                                  if clocks_within_clinic_hours(search_state, [slot["start"]]) != []]

        # إذا لم تتوفر مواعيد للطبيب المفضل،
        # يمكن البحث عند أطباء آخرين في وضع auto فقط.
        if (
            not matching_slots
            and not (closed or outside_hours)
            and provider_choice == "auto"
            and search_state.get("provider_id") is not None
        ):

            # نسخ الحالة دون تقييد البحث بالطبيب المفضل.
            alternative_state = dict(search_state)
            alternative_state["provider_id"] = None

            # تجهيز شروط البحث البديل.
            alternative_arguments = build_find_slots_args(
                alternative_state,
                limit=5,
            )

            # البحث عن مواعيد بديلة بنفس التاريخ والوقت.
            alternative_result = await find_available_slots(
                gateway,
                alternative_arguments,
            )

            # قراءة النتائج والتحقق من مطابقتها.
            alternative_slots = extract_slots(
                alternative_result
            )

            matching_slots = filter_matching_slots(
                alternative_state,
                alternative_slots,
            )

            used_alternatives = bool(matching_slots)
            if matching_slots:
                search_state = alternative_state

        # البحث البديل للوقت المحدد فقط؛ لا نوسع قيد after/before دون طلب المريض.
        if not matching_slots and not (closed or outside_hours) and state.get("time_kind") == "exact":
            requested_hour, requested_minute = map(int, state["requested_time"].split(":"))
            target = requested_hour * 60 + requested_minute
            after, before = max(0, target - 120), min(1440, target + 121)
            near_state = {**search_state, "time_kind": "window", "requested_time": None,
                          "time_after": f"{after // 60:02d}:{after % 60:02d}",
                          "time_before": f"{before // 60:02d}:{before % 60:02d}" if before < 1440 else None}
            near_result = await find_available_slots(gateway, build_find_slots_args(near_state, limit=50))
            nearby = filter_matching_slots(near_state, extract_slots(near_result))
            if not nearby and provider_choice == "auto" and near_state.get("provider_id"):
                near_state["provider_id"] = None
                near_result = await find_available_slots(gateway, build_find_slots_args(near_state, limit=50))
                nearby = filter_matching_slots(near_state, extract_slots(near_result))
                used_alternatives = bool(nearby)
            matching_slots = sorted(nearby, key=lambda slot: (abs(int(slot["start"][:2]) * 60 + int(slot["start"][3:]) - target), slot["start"], str(slot.get("provider_id", "")), str(slot.get("slot_id", ""))))[:5]
            if matching_slots:
                search_state = near_state
                used_time_alternatives = True
            elif weekday in hours:
                day_state = {**near_state, "time_kind": "missing", "time_after": None, "time_before": None}
                day_result = await find_available_slots(gateway, build_find_slots_args(day_state, limit=5))
                matching_slots = [slot for slot in filter_matching_slots(day_state, extract_slots(day_result))
                                  if clocks_within_clinic_hours(day_state, [slot["start"]]) != []]
                if matching_slots:
                    search_state = day_state
                    used_time_alternatives = True

    # التعامل مع استجابة غير صحيحة من العيادة.
    except ValueError:
        return {
            "stage": "scheduler_error",
            "reply": say(state, "I cannot check appointments right now. Please try again.", "مش قادر أتحقق من المواعيد هلأ. جرّب مرة تانية، لو سمحت."),
            "proposed_slots": [],
        }

    # التعامل مع أخطاء الاتصال أو الأدوات.
    except Exception:
        return {
            "stage": "scheduler_error",
            "reply": say(state, "I cannot reach the clinic right now. Please try again.", "التواصل مع العيادة مش متاح هلأ. جرّب مرة تانية، لو سمحت."),
            "proposed_slots": [],
        }

    # عدم اختراع مواعيد عند غياب النتائج.
    if not matching_slots:
        if closed:
            return {"stage": "no_slots", "proposed_slots": [], "reply": say(
                state, "The clinic is closed on your requested day and no later appointments are available.",
                "العيادة مسكّرة باليوم المطلوب وما في مواعيد متاحة بعده.")}
        if state.get("time_outside_hours"):
            return {"stage": "awaiting_time_clarification", "proposed_slots": [], "reply": say(
                state, "That time is outside clinic opening hours. Please choose another time.",
                "هالساعة خارج دوام العيادة. اختار ساعة تانية، لو سمحت.")}
        return {
            "stage": "no_slots",
            "reply": say(state, "No matching appointments are available. Would you like another time?", "ما في موعد متاح بطابق طلبك. بتحب وقت تاني؟"),
            "proposed_slots": [],
        }

    # توضيح أن بعض المواعيد من البحث البديل.
    if used_date_alternatives:
        reply = say(state, "The clinic is closed on your requested day. These later dates are available:\n", "العيادة مسكّرة باليوم المطلوب. هاي خيارات بأقرب يوم لاحق:\n")
    elif outside_hours:
        reply = say(state, "That time is outside clinic opening hours. These times are available:\n",
                    "هالساعة خارج دوام العيادة. هاي مواعيد متاحة:\n")
    elif used_time_alternatives:
        requested_label = display_time(state["requested_time"], state.get("language", "en"))
        if search_state["time_kind"] == "missing":
            reply = say(state, f"{requested_label} is unavailable. These other times are available:\n",
                        f"الساعة {requested_label} مش متاحة. هاي مواعيد تانية متاحة:\n")
        else:
            reply = say(state, f"{requested_label} is unavailable. These nearby times are available:\n",
                        f"الساعة {requested_label} مش متاحة. هاي مواعيد قريبة متاحة:\n")
    elif used_alternatives:
        reply = say(state, "Your preferred doctor is unavailable. These alternatives are available:\n",
                    "ما في موعد عند طبيبك المفضّل. هاي خيارات عند أطباء تانيين:\n")
    else:
        reply = ""

    # نبني العرض من نفس القائمة التي سنحفظها، فلا تتغير دلالة أرقام الخيارات.
    reply += format_slot_options(matching_slots, state)

    # إعادة المواعيد الحقيقية دون إنشاء حجز.
    return {
        "stage": "slots_proposed",
        "reply": reply,
        "proposed_slots": matching_slots,
        "selected_slot_id": None,
        "pending_action": None,
        "selection_candidates": [],
        "offer_context": {"request": scheduling_signature(state), "search": scheduling_signature(search_state),
                          "slot_ids": [slot["slot_id"] for slot in matching_slots]},
    }
