
# slot_handoff.py
# هذا الملف يربط بيانات Intake بمتطلبات Scheduler.
# يجهز معاملات البحث ويتحقق من مطابقة الموعد.
# لا يتصل بالعيادة ولا ينشئ أي حجز.

import re
from datetime import date, datetime, time

from agent.state import AgentState


def clinic_local_now(calendar: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(calendar.get("now"))
    except (TypeError, ValueError):
        return None


def appointment_has_passed(state: AgentState, day: str, clock: str | None = None) -> bool:
    now = clinic_local_now(state.get("clinic_calendar") or {})
    today = now.date().isoformat() if now else state.get("clinic_today")
    if today and day < today:
        return True
    if not now or day != today or clock is None:
        return False
    try:
        return time.fromisoformat(clock) < now.time()
    except (TypeError, ValueError):
        return False


# بصمة القيود المتحققة تربط قائمة البدائل بالطلب الذي أنتجها، ولا تشمل لغة العرض أو بيانات المريض.
def scheduling_signature(state: AgentState) -> dict:
    return {key: state.get(key) for key in ("service_id", "requested_date", "provider_id", "provider_choice",
                                           "time_kind", "requested_time", "time_after", "time_before")}


# تتحقق من اختيار داخل القائمة الحالية. القيود الأصلية تظل صارمة إلا لبديل عرضه Scheduler
# ببحث MCP موثق، وبنفس بصمة الطلب. هذه لا تختار موعدًا ولا تنفذ كتابة.
def slot_matches_offer(state: AgentState, slot: dict) -> bool:
    offered = next((item for item in state.get("proposed_slots", []) if item.get("slot_id") == slot.get("slot_id")), None)
    if offered is None or any(offered.get(key) != slot.get(key) for key in ("date", "start", "provider_id", "service_id")):
        return False
    context = state.get("offer_context") or {}
    if context and context.get("request") != scheduling_signature(state):
        return False
    if slot_matches_request(state, slot):
        return True
    if context.get("request") != scheduling_signature(state) or slot.get("slot_id") not in context.get("slot_ids", []):
        return False
    return slot_matches_request({**state, **context.get("search", {})}, slot)


# تحويل الوقت إلى عدد دقائق منذ منتصف الليل.
def _minutes(value: str) -> int:

    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d",
        value,
    ):
        raise ValueError("Invalid time format")

    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


# تحويل بيانات المريض إلى معاملات find_slots.
def build_find_slots_args(
    state: AgentState,
    limit: int = 8,
) -> dict:

    # لا نبحث قبل جاهزية Intake.
    if state.get("stage") != "needs_slot_lookup":
        raise ValueError("Intake is not ready")

    service_id = state.get("service_id")
    requested_date = state.get("requested_date")

    if not service_id or not requested_date:
        raise ValueError("Service and date are required")

    # التحقق من تنسيق التاريخ.
    date.fromisoformat(requested_date)

    if not 1 <= limit <= 50:
        raise ValueError("Invalid slot limit")

    kind = state.get("time_kind", "missing")
    after = None
    before = None

    # عند وجود وقت محدد نحصر البحث بدقيقة واحدة.
    if kind == "exact":
        exact = state.get("requested_time")

        if not exact:
            raise ValueError("Exact time is missing")

        minute = _minutes(exact)
        after = exact

        if minute < 1439:
            next_minute = minute + 1
            before = (
                f"{next_minute // 60:02d}:"
                f"{next_minute % 60:02d}"
            )

    # دعم فترة بحدين أو بحد واحد فقط.
    elif kind == "window":
        after = state.get("time_after")
        before = state.get("time_before")

        if after is None and before is None:
            raise ValueError("Time window is incomplete")

        if after is not None:
            _minutes(after)

        if before is not None:
            _minutes(before)

        # التأكد من أن البداية تسبق النهاية.
        if after is not None and before is not None:
            if _minutes(after) >= _minutes(before):
                raise ValueError("Invalid time window")

    # إذا لم يحدد المريض وقتا نبحث دون قيود زمنية.
    elif kind == "missing":
        pass

    # منع البحث عندما يكون الوقت غامضا.
    else:
        raise ValueError("Time clarification is required")

    # قراءة تفضيل الطبيب.
    provider_choice = state.get("provider_choice", "auto")
    provider_id = state.get("provider_id")

    if provider_choice == "any":
        provider_id = None

    elif provider_choice == "specific" and not provider_id:
        raise ValueError("Requested provider is not verified")

    # إعادة المعاملات التي يحتاجها MCP.
    return {
        "service_id": service_id,
        "date_from": requested_date,
        "date_to": requested_date,
        "after": after,
        "before": before,
        "provider_id": provider_id,
        "limit": limit,
    }


# التحقق من مطابقة موعد معين لطلب المريض.
def slot_matches_request(
    state: AgentState,
    slot: dict,
) -> bool:

    # التحقق من شكل الموعد ومعرفه.
    if not isinstance(slot, dict):
        return False

    if not slot.get("slot_id"):
        return False

    # لا يمكن التحقق دون خدمة رسمية.
    if not state.get("service_id"):
        return False

    # الخدمة قد تكون موجودة في نتيجة البحث العامة.
    if (
        slot.get("service_id") is not None
        and slot["service_id"] != state["service_id"]
    ):
        return False

    # التحقق من التاريخ.
    if slot.get("date") != state.get("requested_date"):
        return False

    # التحقق من الطبيب المطلوب.
    if state.get("provider_choice") != "any":
        provider_id = state.get("provider_id")

        if provider_id and slot.get("provider_id") != provider_id:
            return False

    # قراءة وقت الموعد والتحقق من تنسيقه.
    try:
        start_minutes = _minutes(slot.get("start"))
    except (ValueError, TypeError):
        return False
    if appointment_has_passed(state, slot["date"], slot["start"]):
        return False

    kind = state.get("time_kind", "missing")

    # مطابقة ساعة محددة.
    if kind == "exact":
        return slot.get("start") == state.get("requested_time")

    # مطابقة فترة زمنية، سواء بحد واحد أو حدين.
    if kind == "window":
        after = state.get("time_after")
        before = state.get("time_before")

        if after is None and before is None:
            return False

        try:
            if after is not None:
                if start_minutes < _minutes(after):
                    return False

            if before is not None:
                if start_minutes >= _minutes(before):
                    return False

        except (ValueError, TypeError):
            return False

        return True

    # قبول أي وقت فقط عندما لا يوجد تفضيل.
    return kind == "missing"
