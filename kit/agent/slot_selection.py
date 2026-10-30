
# agent/slot_selection.py

# هذا الملف مسؤول عن اختيار موعد من المواعيد
# التي أعادها Scheduler عبر Clinic MCP.
#
# لا يبحث عن مواعيد جديدة.
# لا ينفذ أي حجز.
# لا يستدعي LLM.
# يرفض الاختيارات الغامضة أو غير المطابقة للطلب.

import re

from agent.state import AgentState
from agent.slot_handoff import slot_matches_request


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


# اختيار موعد واحد بطريقة آمنة.
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

    # محاولة اختيار الموعد باستخدام Slot ID.
    matches = find_slots_by_id(slots, message)

    # إذا لم يذكر المريض Slot ID، نفحص الوقت.
    if not matches:

        requested_time = extract_time(message)

        if requested_time is None:
            return {
                "stage": "awaiting_slot_selection",
                "selected_slot_id": None,
                "reply": (
                    "Please specify an available appointment "
                    "time or slot ID."
                ),
            }

        matches = [
            slot
            for slot in slots
            if slot.get("start") == requested_time
        ]

        # عند وجود أكثر من طبيب بنفس الساعة،
        # نحاول الاستفادة من اسم الطبيب في الرسالة.
        if len(matches) > 1:
            provider_matches = filter_by_provider(
                matches,
                message,
            )

            if provider_matches:
                matches = provider_matches

    # لا نقبل موعدًا غير موجود في القائمة.
    if not matches:
        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
            "reply": (
                "The selected appointment was not found. "
                "Please choose one of the displayed slots."
            ),
        }

    # لا نختار موعدًا بشكل عشوائي.
    if len(matches) != 1:

        choices = ", ".join(
            (
                f"{slot.get('start')} with "
                f"{slot.get('provider_name')} "
                f"(ID: {slot.get('slot_id')})"
            )
            for slot in matches
        )

        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
            "reply": (
                "Multiple appointments match your choice: "
                f"{choices}. Please specify the slot ID."
            ),
        }

    selected = matches[0]

    # التحقق من تطابق الموعد مع الطلب الأصلي.
    if not slot_matches_request(state, selected):
        return {
            "stage": "awaiting_slot_selection",
            "selected_slot_id": None,
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
