
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
from agent.slot_handoff import slot_matches_request


# تستقبل قائمة المواعيد المصفاة وتعيد نصًا مرقمًا بنفس ترتيبها دون فرز أو نسخ مواعيد.
# الرقم للعرض فقط؛ معرّف الموعد الحقيقي يظل مصدر الاختيار والحجز لاحقًا.
def format_slot_options(slots: list[dict]) -> str:
    return "\n".join(
        f"{index}. {slot['date']} | {slot['start']} | "
        f"{slot.get('provider_name') or slot.get('provider_id', 'Unknown provider')} | "
        f"ID: {slot['slot_id']}"
        for index, slot in enumerate(slots, start=1)
    )


# تستقبل رسالة وتعيد الأرقام المصرح بها؛ None تعني عدم وجود اختيار رقمي.
# القائمة الفارغة تعني صيغة رقمية غير صالحة. لا نفسر الهاتف أو ساعة الموعد كرقم خيار.
# تدعم الأرقام العربية والفارسية؛ التحقق من الحدود يتم مقابل القائمة الحالية فقط.
def extract_option_numbers(message: str) -> list[int] | None:
    text = message.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    number_list = r"[+-]?\d+(?:\.\d+)?(?:\s*(?:,|،|and\b|or\b|و|أو)\s*[+-]?\d+(?:\.\d+)?)*"
    bare = re.fullmatch(rf"\s*({number_list})\s*[.!]?\s*", text, re.IGNORECASE)
    labelled = list(re.finditer(
        rf"(?<!\w)(?:options?|choices?|الخيار|خيار|choose|select|pick|أختار|اختار)"
        rf"\s*(?:number\s*|رقم\s*)?({number_list})(?![\w:])",
        text, re.IGNORECASE,
    ))
    if bare:
        values = [bare.group(1)]
        # الرقم الطويل المنفرد قد يكون هاتف المريض، وليس رقم خيار.
        if re.fullmatch(r"[+]?\d{7,}", values[0]):
            return None
    elif labelled:
        values = [match.group(1) for match in labelled]
    elif re.search(r"\b(?:options?|choices?)\b|الخيار", text, re.IGNORECASE):
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


# محاولة تحديد الطبيب من أسماء الأطباء المعروضة.
def filter_by_provider(
    slots: list[dict],
    message: str,
) -> list[dict]:

    message_lower = message.casefold()

    matches = []

    for slot in slots:
        provider_name = slot.get("provider_name")

        if not isinstance(provider_name, str):
            continue

        # مطابقة الاسم الكامل للطبيب.
        if provider_name.casefold() in message_lower:
            matches.append(slot)

    return matches


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
            "reply": (
                "There are no available slots to select. "
                "Please search for appointments again."
            ),
        }

    # لا نعطي أولوية صامتة لرقم على معرّف أو ساعة؛ يجب أن تتفق كل الإشارات.
    # نعيد عرض القائمة كاملة عند الرفض كي تبقى أرقامها مطابقة للحالة المحفوظة.
    rejected = {
        "stage": "awaiting_slot_selection", "selected_slot_id": None,
        "pending_action": None,
        "reply": "Invalid, conflicting, or ambiguous selection. "
                 "Choose one option number or slot ID from the current list:\n"
                 + format_slot_options(slots),
    }
    if re.search(r"\b(?:old|previous|earlier)\s+(?:list|options?)\b|القائمة\s+القديمة|الخيارات\s+السابقة",
                 message, re.IGNORECASE):
        return rejected
    numbers = extract_option_numbers(message)
    matches = slots
    has_selection = False
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

    time_tokens = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b", message, re.IGNORECASE)
    if time_tokens:
        if len(time_tokens) != 1:
            return rejected
        requested_time = extract_time(time_tokens[0])
        if requested_time is None:
            return rejected
        matches = [slot for slot in matches if slot.get("start") == requested_time]
        has_selection = True

    provider_matches = filter_by_provider(slots, message)
    if provider_matches:
        matches = [slot for slot in matches if slot in provider_matches]

    if not has_selection or len(matches) != 1:
        return rejected

    selected = matches[0]

    # التحقق من تطابق الموعد مع الطلب الأصلي.
    if not slot_matches_request(state, selected):
        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
            "pending_action": None,
            "reply": (
                "This appointment does not match your "
                "verified request. Please search again "
                "or choose another matching slot."
            ),
        }

    # حفظ الاختيار فقط، دون إجراء حجز.
    return {
        "stage": "slot_selected",
        "selected_slot_id": selected["slot_id"],
        "pending_action": None,
        "reply": (
            f"You selected {selected.get('start')} with "
            f"{selected.get('provider_name', 'the doctor')}. "
            "Please provide your full name and phone number."
        ),
    }
