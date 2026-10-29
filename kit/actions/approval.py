
# ==================================================
# تذكير — approval.py
#
# هذا الملف مسؤول عن مرحلة موافقة المريض.
# prepare_booking_action تجهز طلب حجز معلّق.
# لا تنفذ أي حجز ولا تستدعي MCP.
# الحجز الفعلي سيحدث فقط بعد موافقة صريحة.
# action_id سيُستخدم لاحقًا لمنع تكرار الحجز.
# ==================================================

from uuid import uuid4

from agent.state import AgentState
from core.contracts import PendingAction
import asyncio
from time import perf_counter

from core.contracts import ChatResponse, ClinicGateway
from core.session_store import session_store
from agent.slot_handoff import slot_matches_request
# تجهيز طلب حجز يحتاج إلى موافقة المريض.
def prepare_booking_action(state: AgentState) -> PendingAction:

    # التأكد من وجود المعلومات الأساسية
    required_fields = (
        "service_id",
        "selected_slot_id",
        "patient_name",
        "patient_phone",
        "requested_date",
    )

    for field in required_fields:
        if not state.get(field):
            raise ValueError(f"Missing required field: {field}")

    # لا نفعل مسار الأطفال قبل إضافة التحقق من ولي الأمر
    if state["service_id"] == "pediatrics_visit":
        raise ValueError("Guardian verification is required")

    # التأكد أن الموعد المختار موجود في قائمة المواعيد المقترحة
    selected_slot = next(
        (
            slot
            for slot in state.get("proposed_slots", [])
            if slot.get("slot_id") == state["selected_slot_id"]
        ),
        None,
    )

    if selected_slot is None:
        raise ValueError("Selected slot was not found")

    
    # تذكير — التأكد من تطابق تاريخ الموعد.
    if selected_slot.get("date") != state["requested_date"]:
        raise ValueError("Selected slot date does not match")

    # تذكير — التأكد من تطابق الوقت والطبيب والخدمة.
    # لا نجهز طلب موافقة إذا كان الموعد مختلفا.
    if not slot_matches_request(state, selected_slot):
        raise ValueError(
            "Selected slot does not match the patient request"
        )


    # تذكير:
    # لا نجهز طلب موافقة على موعد لا يطابق
    # الوقت أو الطبيب الذي اختاره المريض.
    if not slot_matches_request(state, selected_slot):
        raise ValueError(
            "Selected slot does not match the patient request"
        )
    # تحضير بيانات الحجز دون تنفيذه
    payload = {
        "slot_id": state["selected_slot_id"],
        "service_id": state["service_id"],
        "patient_name": state["patient_name"],
        "patient_phone": state["patient_phone"],
    }

    # إعادة نفس العملية المعلقة إذا لم تتغير تفاصيل الحجز
    existing = state.get("pending_action")

    if (
        existing is not None
        and existing["type"] == "create_booking"
        and existing["payload"] == payload
    ):
        return existing

    # تجهيز وصف واضح لما سيوافق عليه المريض
    summary = (
        f"Book {state['service_id']} "
        f"on {selected_slot['date']} "
        f"at {selected_slot['start']} "
        f"with {selected_slot['provider_name']} "
        f"for {state['patient_name']} "
        f"({state['patient_phone']})"
    )

    # إنشاء عملية معلقة بمعرف فريد
    pending_action: PendingAction = {
        "action_id": str(uuid4()),
        "type": "create_booking",
        "summary": summary,
        "payload": payload,
    }

    return pending_action

# ==================================================
# تذكير — handle_approval
#
# الهدف: التعامل مع قرار المريض بشأن طلب الحجز المعلق.
#
# 1. إذا ضغط المريض Reject:
#    نلغي الطلب المعلق دون تنفيذ أي حجز.
#
# 2. إذا ضغط المريض Approve:
#    نتحقق من الطلب ثم نستدعي create_booking
#    عبر ClinicGateway لتنفيذ الحجز.
#
# 3. إذا ضغط المريض Approve مرتين:
#    نمنع إنشاء حجزين لنفس العملية.
#    نستخدم action_id كمفتاح idempotency_key.
#
# 4. إذا فشل الاتصال بالعيادة:
#    لا ندعي نجاح الحجز دون تأكيد من النظام.
#
# 5. نحفظ نتيجة العملية داخل SessionStore.
#
# ==================================================
_approval_lock = asyncio.Lock()


# تجهيز الرد الذي سيرسل إلى FastAPI.
def _approval_response(
    session_id: str,
    state: AgentState,
    reply: str,
    tool_calls=None,
    result=None,
) -> ChatResponse:

    response: ChatResponse = {
        "session_id": session_id,
        "reply": reply,
        "state": state.get("stage"),
        "tool_calls": tool_calls or [],
    }

    if state.get("pending_action") is not None:
        response["pending_action"] = state["pending_action"]

    if result is not None:
        response["result"] = result

    return response


# تنفيذ قرار المريض بالموافقة أو الرفض.
async def handle_approval(
    session_id: str,
    action_id: str,
    approved: bool,
    gateway: ClinicGateway,
) -> ChatResponse:

    async with _approval_lock:

        # استرجاع حالة المحادثة المحفوظة.
        state = session_store.get_or_create(session_id)

        # العثور على الطلب الذي ينتظر الموافقة.
        pending = state.get("pending_action")

        # منع التعامل مع طلب غير موجود أو بمعرف خاطئ.
        if pending is None or pending["action_id"] != action_id:
            return _approval_response(
                session_id,
                state,
                "No matching pending action. No booking was attempted.",
            )

        # هذه المرحلة تدعم إنشاء الحجز فقط.
        if pending["type"] != "create_booking":
            return _approval_response(
                session_id,
                state,
                "This action type is not supported yet.",
            )

        # الحالة الأولى: المريض رفض.
        if not approved:

            state["pending_action"] = None
            state["selected_slot_id"] = None
            state["stage"] = "booking_rejected"

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                "Your request was rejected. No booking was made.",
            )

        # الحالة الثانية: المريض وافق.
        payload = pending["payload"]

        # التحقق من تطابق بيانات الحجز مع الحالة المحفوظة.
        if (
            payload.get("service_id") != state.get("service_id")
            or payload.get("slot_id") != state.get("selected_slot_id")
            or payload.get("patient_name") != state.get("patient_name")
            or payload.get("patient_phone") != state.get("patient_phone")
        ):
            return _approval_response(
                session_id,
                state,
                "Booking details changed. Please review the appointment again.",
            )

        
        # تذكير:
        # نعيد التحقق عند الموافقة، وليس عند التجهيز فقط.
        # هذا يمنع تنفيذ حجز إذا تغيرت بيانات الطلب.
        valid_slot = next(
            (
                slot
                for slot in state.get("proposed_slots", [])
                if slot.get("slot_id") == payload["slot_id"]
                and slot.get("date") == state.get("requested_date")
            ),
            None,
        )

        if (
            valid_slot is None
            or not slot_matches_request(state, valid_slot)
        ):
            return _approval_response(
                session_id,
                state,
                "The selected appointment must be verified again.",
            )

            return _approval_response(
                session_id,
                state,
                "The selected appointment must be verified again.",
            )

        # تنفيذ الحجز باستخدام معرف العملية نفسه.
        start = perf_counter()

        try:
            booking = await gateway.call_write(
                "create_booking",
                {
                    **payload,
                    "idempotency_key": action_id,
                },
            )
        except Exception:
            booking = {
                "ok": False,
                "error": {"code": "unreachable"},
            }

        duration_ms = round(
            (perf_counter() - start) * 1000,
            2,
        )

        # لا نعتبر الحجز مؤكدًا إلا إذا أعادت العيادة booking_id.
        success = (
            isinstance(booking, dict)
            and booking.get("ok") is True
            and isinstance(booking.get("booking_id"), str)
            and bool(booking["booking_id"])
        )

        tool_calls = [{
            "tool": "create_booking",
            "ok": success,
            "ms": duration_ms,
        }]

        # الحجز نجح.
        if success:

            state["pending_action"] = None
            state["stage"] = "booking_confirmed"

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                f"Your appointment is confirmed. Booking ID: {booking['booking_id']}",
                tool_calls,
                booking,
            )

        # الموعد أصبح محجوزًا لشخص آخر.
        error = booking.get("error", {}) if isinstance(booking, dict) else {}
        error_code = error.get("code") if isinstance(error, dict) else None

        if error_code == "slot_taken":

            state["pending_action"] = None
            state["selected_slot_id"] = None
            state["proposed_slots"] = []
            state["stage"] = "slot_unavailable"

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                "This slot is no longer available. Please choose another appointment.",
                tool_calls,
            )

        # عند تعذر التأكيد نحتفظ بالطلب لإعادة المحاولة بنفس action_id.
        state["stage"] = "booking_unconfirmed"
        session_store.save(state)

        return _approval_response(
            session_id,
            state,
            "The booking could not be confirmed. Please try again.",
            tool_calls,
        )
