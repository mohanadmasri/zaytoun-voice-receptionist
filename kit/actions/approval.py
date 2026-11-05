
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
import logging
from time import perf_counter

from core.contracts import ChatResponse, ClinicGateway
from core.session_store import session_store
from agent.slot_handoff import slot_matches_offer
from agent.presentation import say, booking_summary
from agent.patient_identity import valid_patient_name, normalize_patient_phone
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

    if not valid_patient_name(state.get("patient_name")):
        raise ValueError("The actual patient's full name is required")
    if normalize_patient_phone(state.get("patient_phone")) is None:
        raise ValueError("A valid clinic phone number is required")
    if state.get("confirmed_phone") != state.get("patient_phone"):
        raise ValueError("The recognized phone number must be confirmed first")
    if state.get("date_conflict"):
        raise ValueError("The requested date must be clarified")
    # لا نفعل مسار الأطفال قبل إضافة التحقق من ولي الأمر
    if state.get("patient_age_unverified") or ((state["service_id"] == "pediatrics_visit" or state.get("requires_guardian")) and not valid_patient_name(state.get("guardian_name"))):
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
    if selected_slot.get("date") != state["requested_date"] and not slot_matches_offer(state, selected_slot):
        raise ValueError("Selected slot date does not match")

    # تذكير — التأكد من تطابق الوقت والطبيب والخدمة.
    # لا نجهز طلب موافقة إذا كان الموعد مختلفا.
    if not slot_matches_offer(state, selected_slot):
        raise ValueError(
            "Selected slot does not match the patient request"
        )


    # تذكير:
    # لا نجهز طلب موافقة على موعد لا يطابق
    # الوقت أو الطبيب الذي اختاره المريض.
    if not slot_matches_offer(state, selected_slot):
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
    if state.get("guardian_name") and (state.get("requires_guardian") or state["service_id"] == "pediatrics_visit"):
        payload["guardian_name"] = state["guardian_name"]

    # إعادة نفس العملية المعلقة إذا لم تتغير تفاصيل الحجز
    existing = state.get("pending_action")
    summary = booking_summary(state, selected_slot)
    if payload.get("guardian_name"):
        summary += say(state, f" Guardian: {payload['guardian_name']}.", f" ولي الأمر: {payload['guardian_name']}.")
    memory = {"phone": payload["patient_phone"], "name": payload.get("guardian_name") or payload["patient_name"],
              "language": state.get("language") if state.get("language") in ("ar", "en") else None}
    if state.get("provider_choice") == "specific":
        memory["preferred_provider_id"] = selected_slot.get("provider_id")
        from agent.presentation import provider_label
        label = provider_label(selected_slot, state)
        summary += say(state, f" Remember {label} as your preferred doctor.", f" حفظ {label} كطبيب مفضّل.")
    summary += say(state, " Remember the name and language for future visits.", " حفظ الاسم واللغة للزيارات القادمة.")

    if (
        existing is not None
        and existing["type"] == "create_booking"
        and existing["payload"] == payload
    ):
        return {**existing, "summary": summary}

    # تجهيز وصف واضح لما سيوافق عليه المريض
    # الملخص بلغة الجلسة وأسماء الكتالوج؛ payload ومعرف العملية لا يتغيران بتغيير اللغة.

    # إنشاء عملية معلقة بمعرف فريد
    pending_action: PendingAction = {
        "action_id": str(uuid4()),
        "type": "create_booking",
        "summary": summary,
        "payload": payload,
        "memory": memory,
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
        "language": state.get("language", "en"),
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
                say(state, "No matching pending action. No booking was attempted.", "ما في طلب موافقة مطابق. ما تم أي حجز."),
            )

        # هذه المرحلة تدعم إنشاء الحجز فقط.
        if pending["type"] == "cancel_booking":
            from actions.cancellation import approve_cancellation
            return await approve_cancellation(state, pending, approved, gateway)
        if pending["type"] != "create_booking":
            return _approval_response(
                session_id,
                state,
                say(state, "This action type is not supported yet.", "هالعملية مش مدعومة حاليًا."),
            )

        # الحالة الأولى: المريض رفض.
        if not approved:

            state["pending_action"] = None
            state["selected_slot_id"] = None
            state["stage"] = "booking_rejected"
            state["proposed_slots"] = []
            state["offer_context"] = None

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                say(state, "Your request was rejected. No booking was made.", "تم إلغاء الطلب، وما تم أي حجز. بتحب نبحث عن موعد تاني؟"),
            )

        # الحالة الثانية: المريض وافق.
        payload = pending["payload"]
        if state.get("confirmed_phone") != state.get("patient_phone"):
            state.update({"pending_action": None, "stage": "awaiting_phone_confirmation"})
            session_store.save(state)
            return _approval_response(session_id, state, say(state, "Please confirm the recognized phone number before reviewing this booking.", "أكد رقم التلفون المسموع قبل مراجعة الحجز، لو سمحت."))
        if normalize_patient_phone(state.get("patient_phone")) is None:
            state.update({"pending_action": None, "patient_phone": None, "patient_phone_invalid": True, "stage": "awaiting_patient_details"})
            session_store.save(state)
            return _approval_response(session_id, state, say(state, "Please provide a valid phone number starting with 0 or +970/+972. No booking was made.", "أعطيني رقم تلفون صحيح يبدأ بصفر أو +970/+972. ما تم أي حجز."))
        guardian_required = state.get("requires_guardian") or state.get("service_id") == "pediatrics_visit"
        if not valid_patient_name(state.get("patient_name")) or (guardian_required and not valid_patient_name(state.get("guardian_name"))) or state.get("patient_age_unverified") or state.get("date_conflict"):
            state["pending_action"] = None
            state["stage"] = "awaiting_patient_details"
            session_store.save(state)
            return _approval_response(session_id, state, say(state, "Please verify the patient's full name and guardian details.", "لازم نتحقق من اسم المريض الكامل وبيانات ولي الأمر."))

        # التحقق من تطابق بيانات الحجز مع الحالة المحفوظة.
        if (
            payload.get("service_id") != state.get("service_id")
            or payload.get("slot_id") != state.get("selected_slot_id")
            or payload.get("patient_name") != state.get("patient_name")
            or payload.get("patient_phone") != state.get("patient_phone")
            or payload.get("guardian_name") != (state.get("guardian_name") if guardian_required else None)
        ):
            return _approval_response(
                session_id,
                state,
                say(state, "Booking details changed. Please review the appointment again.", "تفاصيل الموعد تغيّرت. راجعها مرة تانية، لو سمحت."),
            )

        
        # تذكير:
        # نعيد التحقق عند الموافقة، وليس عند التجهيز فقط.
        # هذا يمنع تنفيذ حجز إذا تغيرت بيانات الطلب.
        valid_slot = next(
            (
                slot
                for slot in state.get("proposed_slots", [])
                if slot.get("slot_id") == payload["slot_id"]
            ),
            None,
        )

        if (
            valid_slot is None
            or not slot_matches_offer(state, valid_slot)
        ):
            return _approval_response(
                session_id,
                state,
                say(state, "The selected appointment must be verified again.", "لازم نتحقق من الموعد المختار مرة تانية."),
            )

            return _approval_response(
                session_id,
                state,
                "The selected appointment must be verified again.",
            )

        # تنفيذ الحجز باستخدام معرف العملية نفسه.
        # فحص السعة الموثقة قبل الكتابة؛ لا نعامل عطل الذاكرة كمريض جديد أو نتجاوز الحد.
        maximum = ((state.get("clinic_calendar") or {}).get("policies") or {}).get("max_upcoming_bookings_per_patient")
        if isinstance(maximum, int) and maximum > 0:
            try:
                patient = await gateway.call_read("get_patient", {"phone": payload["patient_phone"]})
            except Exception:
                patient = {"ok": False}
            if patient.get("ok") and isinstance(patient.get("upcoming_bookings"), list):
                if len(patient["upcoming_bookings"]) >= maximum:
                    state.update({"pending_action": None, "stage": "awaiting_patient_details"})
                    session_store.save(state)
                    return _approval_response(session_id, state, say(state,
                        f"The clinic allows at most {maximum} upcoming appointments per phone. Please contact the clinic; no new booking was made.",
                        f"العيادة بتسمح بحد أقصى {maximum} مواعيد قادمة لكل تلفون. تواصل مع العيادة؛ ما تم حجز جديد."))
            elif patient.get("error", {}).get("code") != "unknown_patient":
                return _approval_response(session_id, state, say(state, "I could not verify the clinic policy. Please try again; no booking was attempted.", "ما قدرت أتحقق من سياسة العيادة. جرّب مرة تانية؛ ما حاولنا نحجز."))
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

            # الذاكرة جزء ظاهر من الموافقة؛ لا حفظ قبل نجاح الحجز ولا إعادة حجز إن فشل حفظ الذاكرة.
            if pending.get("memory"):
                memory_started = perf_counter()
                try:
                    saved = await gateway.call_write("save_patient", pending["memory"])
                    memory_ok = isinstance(saved, dict) and saved.get("ok") is True
                except Exception:
                    memory_ok = False
                tool_calls.append({"tool": "save_patient", "ok": memory_ok, "ms": round((perf_counter() - memory_started) * 1000, 2)})

            state["pending_action"] = None
            state["stage"] = "booking_confirmed"
            state["proposed_slots"] = []
            state["selected_slot_id"] = None
            state["offer_context"] = None

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                say(state, f"Your appointment is confirmed. Booking ID: {booking['booking_id']}", f"تم تأكيد موعدك. رقم الحجز: {booking['booking_id']}") + (say(state, " Your visit is booked, but we could not save your preferences.", " الموعد مؤكد، لكن تعذر حفظ التفضيلات.") if pending.get("memory") and not memory_ok else ""),
                tool_calls,
                booking,
            )

        # الموعد أصبح محجوزًا لشخص آخر.
        error = booking.get("error", {}) if isinstance(booking, dict) else {}
        error_code = error.get("code") if isinstance(error, dict) else None
        if error_code == "policy_violation" or (isinstance(booking, dict) and booking.get("status") == 422):
            from integrations.mcp_gateway import sanitized_clinic_error
            detail = sanitized_clinic_error(booking, payload)
            state.update({"pending_action": None, "stage": "awaiting_patient_details"})
            session_store.save(state)
            response = _approval_response(session_id, state, say(state,
                "The clinic rejected this booking under its policy. Review the details or contact the clinic; no booking was confirmed.",
                "العيادة رفضت الحجز حسب سياستها. راجع التفاصيل أو تواصل مع العيادة؛ ما تأكد أي حجز."), tool_calls)
            response["validation_error"] = detail
            return response
        if error_code in ("invalid_phone", "invalid_request"):
            # لا نسجل رسالة العيادة الخام، لأنها قد تعيد رقم الهاتف داخل النص.
            logging.getLogger(__name__).warning("booking_validation_rejected code=%s", error_code)
            state.update({"pending_action": None, "stage": "awaiting_patient_details"})
            if error_code == "invalid_phone":
                state["patient_phone"] = None
                state["patient_phone_invalid"] = True
            session_store.save(state)
            return _approval_response(session_id, state, say(state,
                f"The clinic rejected the booking details ({error_code}). Please correct the details and review again. No booking was made.",
                f"العيادة رفضت بيانات الحجز ({error_code}). صحح البيانات وراجعها مرة تانية. ما تم أي حجز."), tool_calls)

        if error_code == "slot_taken":

            state["pending_action"] = None
            state["selected_slot_id"] = None
            state["proposed_slots"] = []
            state["stage"] = "slot_unavailable"

            session_store.save(state)

            return _approval_response(
                session_id,
                state,
                say(state, "This slot is no longer available. Please choose another appointment.", "هالموعد ما عاد متاح. خلينا نختار موعد تاني."),
                tool_calls,
            )

        # عند تعذر التأكيد نحتفظ بالطلب لإعادة المحاولة بنفس action_id.
        state["stage"] = "booking_unconfirmed"
        session_store.save(state)

        return _approval_response(
            session_id,
            state,
            say(state, "The booking could not be confirmed. Please try again.", "ما قدرنا نؤكد الحجز. جرّب مرة تانية، لو سمحت."),
            tool_calls,
        )
