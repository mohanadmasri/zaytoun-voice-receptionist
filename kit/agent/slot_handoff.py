# ==================================================
# تذكير — slot_handoff.py
#
# وظيفة الملف:
# حلقة وصل بين Intake Agent وScheduler.
# يجهز بيانات البحث عن المواعيد ويتحقق من أن
# الموعد المقترح يطابق طلب المريض.
#
# 1. build_find_slots_args:
# تحول بيانات AgentState إلى معاملات جاهزة
# لأداة find_slots، مثل التاريخ والوقت والطبيب.
# وترفض تجهيز البحث إذا كانت البيانات غير واضحة.
#
# 2. slot_matches_request:
# تفحص الموعد المقترح وتتأكد من مطابقته
# للخدمة والتاريخ والوقت والطبيب المطلوب.
# تعيد True عند التطابق وFalse عند الاختلاف.
#
# ملاحظة:
# الملف لا يبحث عن المواعيد ولا ينفذ الحجوزات.
# البحث الفعلي مسؤولية Scheduler.
# ==================================================

import re
from datetime import date

from agent.state import AgentState


# تحويل HH:MM إلى عدد الدقائق منذ منتصف الليل.
def _minutes(value: str) -> int:

    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d",
        value
    ):
        raise ValueError("Invalid time format")

    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


# تجهيز معاملات find_slots.
def build_find_slots_args(
    state: AgentState,
    limit: int = 8
) -> dict:

    # لا نبحث قبل اكتمال مرحلة Intake.
    if state.get("stage") != "needs_slot_lookup":
        raise ValueError("Intake is not ready")

    service_id = state.get("service_id")
    requested_date = state.get("requested_date")

    if not service_id or not requested_date:
        raise ValueError("Service and date are required")

    # التأكد من صحة صيغة التاريخ.
    date.fromisoformat(requested_date)

    if not 1 <= limit <= 50:
        raise ValueError("Invalid slot limit")

    kind = state.get("time_kind", "missing")

    after = None
    before = None

    # المريض طلب ساعة محددة.
    if kind == "exact":

        exact = state.get("requested_time")

        if not exact:
            raise ValueError("Exact time is missing")

        minute = _minutes(exact)

        after = exact

        # before حد حصري.
        # دقيقة واحدة تضمن البحث عن الساعة نفسها فقط.
        if minute < 1439:
            next_minute = minute + 1
            before = (
                f"{next_minute // 60:02d}:"
                f"{next_minute % 60:02d}"
            )

    # المريض طلب فترة، مثل الصباح.
    elif kind == "window":

        after = state.get("time_after")
        before = state.get("time_before")

        if not after or not before:
            raise ValueError("Time window is incomplete")

        if _minutes(after) >= _minutes(before):
            raise ValueError("Invalid time window")

    # المريض يقبل أي وقت متاح.
    elif kind == "missing":
        pass

    # لا نخمن الوقت الغامض.
    else:
        raise ValueError("Time clarification is required")

    # في وضع any يجب عدم تقييد الطبيب.
    provider_choice = state.get("provider_choice", "auto")

    provider_id = state.get("provider_id")

    if provider_choice == "any":
        provider_id = None

    elif provider_choice == "specific" and not provider_id:
        raise ValueError("Requested provider is not verified")

    return {
        "service_id": service_id,
        "date_from": requested_date,
        "date_to": requested_date,
        "after": after,
        "before": before,
        "provider_id": provider_id,
        "limit": limit,
    }


# التأكد من مطابقة الموعد لطلب المريض.
def slot_matches_request(
    state: AgentState,
    slot: dict
) -> bool:

    # لا نقبل موعدا غير موثق بالبيانات الأساسية.
    if not isinstance(slot, dict):
        return False

    if not slot.get("slot_id"):
        return False

        # تذكير:
    # نظام العيادة يضع service_id في نتيجة البحث العامة،
    # وليس بالضرورة داخل كل slot.
    if not state.get("service_id"):
        return False

    # إذا كان معرف الخدمة موجودا في الموعد، نتحقق منه.
    if (
        slot.get("service_id") is not None
        and slot["service_id"] != state["service_id"]
    ):
        return False

    if slot.get("date") != state.get("requested_date"):
        return False

    # التحقق من الطبيب عند وجود اختيار محدد.
    if state.get("provider_choice") != "any":
        provider_id = state.get("provider_id")

        if provider_id and slot.get("provider_id") != provider_id:
            return False

    # التحقق من وقت الموعد.
    start = slot.get("start")

    try:
        start_minutes = _minutes(start)
    except ValueError:
        return False

    kind = state.get("time_kind", "missing")

    if kind == "exact":
        return start == state.get("requested_time")

    if kind == "window":
        try:
            after = _minutes(state["time_after"])
            before = _minutes(state["time_before"])
        except (ValueError, KeyError, TypeError):
            return False

        return after <= start_minutes < before

    return kind == "missing"
