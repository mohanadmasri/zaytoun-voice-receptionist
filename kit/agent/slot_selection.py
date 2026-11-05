
# agent/slot_selection.py

# هذا الملف مسؤول عن اختيار موعد من المواعيد
# التي أعادها Scheduler عبر Clinic MCP.
#
# لا يبحث عن مواعيد جديدة.
# لا ينفذ أي حجز.
# لا يستدعي LLM.
# يرفض الاختيارات الغامضة أو غير المطابقة للطلب.
# المدخل رسالة وقائمة proposed_slots الحالية؛ المخرج اختيار أو طلب توضيح.

import re

from agent.state import AgentState
from agent.slot_handoff import slot_matches_offer
from agent.presentation import say, display_date, display_time, provider_label
from agent.times import normalize_clock_words, period_reply


# تستقبل قائمة المواعيد المصفاة وتعيد نصًا مرقمًا بنفس ترتيبها دون فرز أو نسخ مواعيد.
# نذكر التاريخ مرة واحدة ونخفي المعرفات؛ الرقم للعرض والمعرف الحقيقي يبقى داخل الحالة.
def format_slot_options(slots: list[dict], state: dict | None = None) -> str:
    state = state or {}
    language = state.get("language", "en")
    day = display_date(slots[0]["date"], language) if slots else ""
    header = say(state, f"Available appointments for {day}:", f"لقيت مواعيد متاحة ليوم {day}:")
    lines = [f"{index}. {display_time(slot['start'], language)} "
             + say(state, "with ", "مع ") + provider_label(slot, state)
             for index, slot in enumerate(slots, start=1)]
    return "\n".join([header, *lines, say(state, "Which one would you prefer?", "أي موعد بناسبك؟")])


# تحول كلمات الأرقام في عبارة اختيار فقط إلى أرقام، وتعيد نسخة للتحليل المحلي.
# لا تغير transcript أو current_message، ولا تحول أرقام الهاتف أو الوقت داخل جمل أخرى.
# تشمل قائمة الأرقام المتعارضة أيضًا: option two or three تصبح option 2 or 3 ليتم رفضها.
def normalize_spoken_options(message: str) -> str:
    words = {
        "zero": 0, "one": 1, "first": 1, "two": 2, "second": 2,
        "three": 3, "third": 3, "four": 4, "fourth": 4, "five": 5, "fifth": 5,
        "six": 6, "sixth": 6, "seven": 7, "seventh": 7, "eight": 8, "eighth": 8,
        "nine": 9, "ninth": 9, "ten": 10, "tenth": 10,
        "صفر": 0, "واحد": 1, "الأول": 1, "الاول": 1, "الأولى": 1, "الاولى": 1,
        "اثنين": 2, "إثنين": 2, "اتنين": 2, "الثاني": 2, "الثانية": 2, "التاني": 2,
        "ثلاثة": 3, "ثلاث": 3, "الثالث": 3, "الثالثة": 3,
        "أربعة": 4, "اربعة": 4, "الرابع": 4, "الرابعة": 4,
        "خمسة": 5, "الخامس": 5, "الخامسة": 5, "ستة": 6, "السادس": 6,
        "سبعة": 7, "السابع": 7, "ثمانية": 8, "الثامن": 8,
        "تسعة": 9, "التاسع": 9, "عشرة": 10, "العاشر": 10,
    }
    text = re.sub(r"[\u064b-\u065f\u0670]", "", message)
    word_pattern = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    item = rf"(?:[+-]?\d+(?:\.\d+)?|(?:{word_pattern}))(?!\w)"
    items = rf"{item}(?:\s*(?:,|،|and\b|or\b|و|أو)\s*{item})*"
    prefix = r"(?:options?|choices?|appointment|choose|select|pick|الموعد|الخيار|خيار|أختار|اختار)\s*(?:number\s*|رقم\s*)?"
    text = re.sub(rf"\bthe\s+({word_pattern})\s+(?:one|appointment|option)\b",
                  r"option \1", text, flags=re.IGNORECASE)

    # يستبدل الكلمات داخل مقطع الاختيار المطابق فقط، مع إبقاء الروابط لكشف التعارض.
    def replace_words(match):
        return re.sub(rf"(?<!\w)(?:{word_pattern})(?!\w)",
                      lambda token: str(words[token.group().casefold()]), match.group(), flags=re.IGNORECASE)

    if re.fullmatch(rf"\s*{items}\s*[.!؟?]?\s*", text, re.IGNORECASE):
        text = re.sub(rf"{items}", replace_words, text, flags=re.IGNORECASE)
    else:
        text = re.sub(rf"(?<!\w){prefix}{items}", replace_words, text, flags=re.IGNORECASE)
    return text


# تستقبل رسالة وتعيد الأرقام المصرح بها؛ None تعني عدم وجود اختيار رقمي.
# القائمة الفارغة تعني صيغة رقمية غير صالحة. لا نفسر الهاتف أو ساعة الموعد كرقم خيار.
# تدعم الأرقام العربية والفارسية؛ التحقق من الحدود يتم مقابل القائمة الحالية فقط.
def extract_option_numbers(message: str) -> list[int] | None:
    text = normalize_spoken_options(message).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    number_list = r"[+-]?\d+(?:\.\d+)?(?:\s*(?:,|،|and\b|or\b|و|أو)\s*[+-]?\d+(?:\.\d+)?)*"
    bare = re.fullmatch(rf"\s*({number_list})\s*[.!]?\s*", text, re.IGNORECASE)
    labelled = list(re.finditer(
        rf"(?<!\w)(?:options?|choices?|appointment|الموعد|الخيار|خيار|choose|select|pick|أختار|اختار)"
        rf"\s*(?:number\s*|رقم\s*)?({number_list})(?![\w:])",
        text, re.IGNORECASE,
    ))
    if bare:
        values = [bare.group(1)]
        # الرقم الطويل المنفرد قد يكون هاتف المريض، وليس رقم خيار.
        if re.fullmatch(r"[+]?\d{7,}", values[0]):
            return None
    elif labelled:
        # لا نتجاهل بديلًا لم نفهم رقمه مثل option two or twenty ونختار الأول صامتًا.
        if any(re.match(r"\s*(?:or\b|أو)(?:\s|$)", text[match.end():], re.IGNORECASE)
               for match in labelled):
            return []
        values = [match.group(1) for match in labelled]
    elif re.search(r"\b(?:options?|choices?)\b|الخيار", text, re.IGNORECASE):
        if re.search(r"\b(?:option|choice)\s+with\b|الخيار\s+(?:مع|عند)", text, re.I):
            return None
        return []
    else:
        return None
    tokens = [token for value in values for token in re.findall(r"[+-]?\d+(?:\.\d+)?", value)]
    if any("." in token or len(token) > 6 for token in tokens):
        return []
    return [int(token) for token in tokens]


# استخراج وقت صحيح من رسالة المريض.
def extract_time(message: str) -> str | None:

    match = re.search(
        r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b",
        message,
        re.IGNORECASE,
    )

    if match is None:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    period = match.group(3)

    # رفض الساعات والدقائق غير الصحيحة.
    if minute > 59:
        return None

    if period is not None:

        # صيغة 12 ساعة تحتاج رقمًا من 1 إلى 12.
        if not 1 <= hour <= 12:
            return None

        hour = hour % 12

        if period.upper() == "PM":
            hour += 12

    elif hour > 23:
        return None

    # توحيد شكل الوقت للمقارنة مع MCP.
    return f"{hour:02d}:{minute:02d}"


# العثور على المواعيد التي تطابق Slot ID كاملًا.
def find_slots_by_id(
    slots: list[dict],
    message: str,
) -> list[dict]:

    matches = []

    for slot in slots:
        slot_id = slot.get("slot_id")

        if not isinstance(slot_id, str) or not slot_id:
            continue

        # منع الخلط بين SL-1 و SL-10.
        pattern = (
            r"(?<![A-Za-z0-9_-])"
            + re.escape(slot_id)
            + r"(?![A-Za-z0-9_-])"
        )

        if re.search(pattern, message, re.IGNORECASE):
            matches.append(slot)

    return matches


# تطابق اسمًا جزئيًا عربيًا أو إنجليزيًا مع أسماء الأطباء الموثقة في القائمة والكتالوج.
# تعيد جميع المتعادلين بأعلى تطابق، حتى لا يتحول اسم مشترك إلى اختيار عشوائي.
def filter_by_provider(
    slots: list[dict],
    message: str,
    state: dict | None = None,
) -> list[dict]:
    from agent.service_mapping import normalize_service_text
    state = state or {}
    titles = {"dr", "doctor", "د", "دكتور", "دكتورة", "الدكتور", "الدكتورة", "طبيب", "طبيبة", "الطبيب", "الطبيبة"}
    # الاسم الذي يأتي بعد «اسمي» هو اسم المريض لا اسم الطبيب، حتى لو كانا متشابهين.
    provider_message = re.split(r"اسمي|رقمي|my name|phone", message, maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = set(normalize_service_text(provider_message).split()) - titles
    scores = []
    for slot in slots:
        provider = next((p for p in state.get("provider_catalog", []) if p.get("id") == slot.get("provider_id")), {})
        names = " ".join(str(name or "") for name in (slot.get("provider_name"), slot.get("provider_name_ar"),
                                                        provider.get("name"), provider.get("name_ar")))
        name_tokens = set(normalize_service_text(names).split()) - titles
        scores.append(len(tokens & name_tokens))
    best = max(scores, default=0)
    return [slot for slot, score in zip(slots, scores) if best and score == best]


# تكتشف إشارة اختيار من القائمة الحالية؛ تستثني تقديم الاسم والهاتف من مطابقة اسم الطبيب.
def has_slot_reference(state: dict, message: str) -> bool:
    return bool(single_option_reference(state, message) or (state.get("proposed_slots") and re.search(r"\b(?:dr|doctor)\b|دكتور|دكتورة", message, re.I)) or (period_reply(message) and state.get("selection_candidates")) or extract_option_numbers(message) is not None or re.search(r"\d{1,2}:\d{2}", normalize_clock_words(message))
                or "SL-" in message.upper()
                or (filter_by_provider(state.get("proposed_slots", []), message, state)
                    and not re.search(r"اسمي|رقمي|my name|phone", message, re.IGNORECASE)))


# الإشارة إلى هذا الموعد لا تُفهم كاختيار إلا إذا بقي خيار واحد فعلي في القائمة الحالية.
def single_option_reference(state: dict, message: str) -> bool:
    return len(state.get("proposed_slots", [])) == 1 and bool(re.fullmatch(
        r"\s*(?:(?:(?:I want|I choose|I would like)\s+)?(?:this option|that appointment|this appointment|that option)|(?:بدي\s+)?(?:هاد الموعد|هذا الموعد|هالخيار|هذا الخيار))\s*[.!؟?]?\s*", message, re.I))


# تعيد الساعات المحتملة لوقت غير موسوم بالصباح/المساء، ولا تختار إلا من المواعيد المعروضة.
# عند وجود 03:45 و15:45 معًا يبقى الاختيار غامضًا بدل افتراض فترة زمنية.
def offered_clock_times(message: str) -> list[str] | None:
    text = normalize_clock_words(message).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    tokens = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b", text, re.IGNORECASE)
    if not tokens:
        return None
    if len(tokens) != 1:
        return []
    value = extract_time(tokens[0])
    if value is None:
        return []
    hour, minute = map(int, value.split(":"))
    if re.search(r"AM|PM", tokens[0], re.IGNORECASE) or hour > 12 or tokens[0].startswith("0"):
        return [value]
    am, pm = bool(re.search(r"صباح|الصبح", text)), bool(re.search(r"مساء|العصر|بعد الظهر", text))
    if am and pm:
        return []
    if pm:
        return [f"{hour % 12 + 12:02d}:{minute:02d}"]
    if am:
        return [f"{hour % 12:02d}:{minute:02d}"]
    return [f"{hour % 12:02d}:{minute:02d}", f"{hour % 12 + 12:02d}:{minute:02d}"]


# تستقبل state التي تحتوي القائمة الحالية وmessage التي تحمل اختيار المريض.
# تعيد تحديثات الاختيار أو طلب توضيح؛ الرقم يبدأ من 1 ولا يُحفظ كمعرّف حجز.
# تتحقق من اتفاق الرقم والمعرف والوقت ومن مطابقة الموعد للطلب قبل حفظ slot_id.
def select_slot(
    state: AgentState,
    message: str,
) -> dict:

    # قراءة المواعيد الحقيقية من حالة المحادثة.
    slots = state.get("proposed_slots") or []

    if not slots:
        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
            "pending_action": None, "selection_candidates": [],
            "reply": say(state, "Please search for appointments first.", "خلينا نبحث عن مواعيد متاحة أولًا."),
        }

    # لا نعطي أولوية صامتة لرقم على معرّف أو ساعة؛ يجب أن تتفق كل الإشارات.
    # نعيد عرض القائمة كاملة عند الرفض كي تبقى أرقامها مطابقة للحالة المحفوظة.
    rejected = {
        "stage": "awaiting_slot_selection", "selected_slot_id": None,
        "pending_action": None, "selection_candidates": [],
        "reply": say(state, "Please choose one of the displayed option numbers.", "اختار رقم موعد واحد من القائمة المعروضة، لو سمحت."),
    }
    if re.search(r"\b(?:old|previous|earlier)\s+(?:list|options?)\b|القائمة\s+القديمة|الخيارات\s+السابقة",
                 message, re.IGNORECASE):
        return rejected
    numbers = extract_option_numbers(message)
    if single_option_reference(state, message):
        numbers = None
    matches = slots
    has_selection = False
    if single_option_reference(state, message):
        has_selection = True
    period = period_reply(message)
    if period and state.get("selection_candidates"):
        matches = [slot for slot in slots if slot["slot_id"] in state["selection_candidates"]
                   and ((int(slot["start"][:2]) < 12) == (period == "AM"))]
        has_selection = True
    if numbers is not None:
        if len(numbers) != 1 or not 1 <= numbers[0] <= len(slots):
            return rejected
        matches = [slots[numbers[0] - 1]]
        has_selection = True

    id_matches = find_slots_by_id(slots, message)
    # معرّف SL قديم أو مجهول لا يجوز تجاهله حتى لو رافقه رقم خيار صحيح.
    mentioned_ids = re.findall(r"(?<![\w-])SL-[A-Za-z0-9_-]+", message, re.IGNORECASE)
    known_ids = {str(slot["slot_id"]).casefold() for slot in slots}
    if any(slot_id.casefold() not in known_ids for slot_id in mentioned_ids):
        return rejected
    if id_matches:
        if len(id_matches) != 1:
            return rejected
        matches = [slot for slot in matches if slot is id_matches[0]]
        has_selection = True

    clock_times = offered_clock_times(message)
    if clock_times is not None:
        if not clock_times:
            return rejected
        matches = [slot for slot in matches if slot.get("start") in clock_times]
        has_selection = True

    provider_matches = filter_by_provider(slots, message, state)
    if provider_matches:
        if not has_selection and state.get("selection_candidates"):
            matches = [slot for slot in matches if slot["slot_id"] in state["selection_candidates"]]
        matches = [slot for slot in matches if slot in provider_matches]
        has_selection = True
    elif re.search(r"\b(?:dr|doctor)\b|دكتور|دكتورة|طبيب|طبيبة", message, re.IGNORECASE):
        return rejected

    if has_selection and not matches and clock_times is not None and provider_matches:
        return {**rejected, "reply": say(state, "That time and doctor are not together in the displayed options. Which option would you prefer?",
                                        "هالساعة مع هالطبيب مش ضمن الخيارات المعروضة. أي خيار بناسبك؟")}
    if not has_selection or not matches:
        return rejected
    if len(matches) > 1:
        doctors = list(dict.fromkeys(provider_label(slot, state) for slot in matches))
        question = say(state, "Which doctor would you prefer: ", "عند أي طبيب بتفضّل: ") + "، ".join(doctors) + "؟"
        if len(doctors) == 1:
            question = say(state, "More than one time is available with this doctor. Which time would you prefer?",
                           "في أكتر من وقت متاح عند هالطبيب. أي ساعة بتناسبك؟")
        return {**rejected, "selection_candidates": [slot["slot_id"] for slot in matches], "reply": question}

    selected = matches[0]

    # التحقق من تطابق الموعد مع الطلب الأصلي.
    if not slot_matches_offer(state, selected):
        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
            "pending_action": None,
            "selection_candidates": [],
            "reply": say(state, "This appointment does not match your request. Please choose another.",
                         "هالموعد ما بطابق طلبك. اختار موعد تاني، لو سمحت."),
        }

    # حفظ الاختيار فقط، دون إجراء حجز.
    return {
        "stage": "slot_selected",
        "selected_slot_id": selected["slot_id"],
        "pending_action": None,
        "selection_candidates": [],
        "reply": say(state, "Appointment selected.", "تم اختيار الموعد."),
    }
