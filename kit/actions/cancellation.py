# سياسة إلغاء واحدة خلف الموافقة؛ الحجز والهاتف ووقت العيادة تأتي من MCP لا من التخمين.
from datetime import datetime
from uuid import uuid4
from agent.presentation import say, display_date, display_time
from core.session_store import session_store


# الهاتف تعريف للحجز وليس تحقق هوية رسميًا؛ نفشل مغلقًا عند غياب الوقت أو اختلاف المالك.
async def checked_booking(state, gateway):
    booking = await gateway.call_read("get_booking", {"booking_id": state["cancellation_booking_id"]})
    clinic = await gateway.call_read("clinic_info", {})
    if not booking.get("ok") or not clinic.get("ok"):
        raise ValueError("unavailable")
    if booking.get("booking_id") != state["cancellation_booking_id"] or booking.get("patient", {}).get("phone") != state.get("patient_phone"):
        raise ValueError("ownership")
    if booking.get("status") != "confirmed":
        raise ValueError("inactive")
    start, now = datetime.fromisoformat(booking["start_at"]), datetime.fromisoformat(clinic["now"])
    if start.tzinfo is None or now.tzinfo is None:
        raise ValueError("unavailable")
    minimum = clinic.get("policies", {}).get("cancellation_min_hours", 24)
    if (start - now).total_seconds() < minimum * 3600:
        raise ValueError("too_late")
    return booking


# التحضير لا يكتب؛ يقرأ ملخص الموعد ويطلب الموافقة على إلغاء واحد بعينه.
async def propose_cancellation(state, gateway):
    if not state.get("cancellation_booking_id") or not state.get("patient_phone"):
        return {"stage": "awaiting_cancellation_details", "pending_action": None,
                "reply": say(state, "Please provide the booking reference and its phone number.", "أعطيني رقم الحجز ورقم التلفون المرتبط فيه، لو سمحت.")}
    try:
        booking = await checked_booking(state, gateway)
    except (ValueError, KeyError, TypeError) as error:
        late = str(error) == "too_late"
        return {"stage": "cancellation_unavailable", "pending_action": None,
                "reply": say(state, "Cancellation within 24 hours is not allowed. Please call the clinic." if late else "I could not verify this cancellation. Check the reference and phone or call the clinic.",
                             "الإلغاء قبل الموعد بأقل من24ساعة مش مسموح. تواصل مع العيادة." if late else "ما قدرت أتحقق من الإلغاء. تأكد من المرجع والتلفون أو تواصل مع العيادة.")}
    day, clock = display_date(booking["date"], state.get("language")), display_time(booking["start"], state.get("language"))
    summary = say(state,
        f"Cancel {booking.get('service_name', 'the appointment')} on {day} at {clock} with {booking.get('provider_name', 'the doctor')} for {booking['patient']['name']} ({state['patient_phone']}).",
        f"إلغاء {booking.get('service_name_ar') or booking.get('service_name', 'الموعد')} يوم {day} الساعة {clock} عند {booking.get('provider_name_ar') or booking.get('provider_name', 'الطبيب')} للمريض {booking['patient']['name']} ({state['patient_phone']}).")
    payload = {"booking_id": booking["booking_id"], "reason": "Patient approved cancellation"}
    existing = state.get("pending_action") or {}
    pending = existing if existing.get("type") == "cancel_booking" and existing.get("payload") == payload else {
        "action_id": str(uuid4()), "type": "cancel_booking", "summary": summary, "payload": payload}
    return {"stage": "awaiting_approval", "cancellation_record": booking,
            "pending_action": pending,
            "reply": say(state, "Review and approve: ", "راجع ووافق: ") + summary}


# يعيد التحقق عند الموافقة؛ لا يكرر كتابة ناجحة، ولا يلغي إن تغير الموعد أو اقترب دون24ساعة.
async def approve_cancellation(state, pending, approved, gateway):
    from actions.approval import _approval_response
    if not approved:
        state.update({"pending_action": None, "stage": "booking_rejected"})
        session_store.save(state)
        return _approval_response(state["session_id"], state, say(state, "Cancellation rejected. Nothing was cancelled.", "رفضت الإلغاء، وما انلغى أي موعد."))
    try:
        booking = await checked_booking(state, gateway)
        previous = state.get("cancellation_record") or {}
        if pending["payload"]["booking_id"] != booking["booking_id"] or any(booking.get(k) != previous.get(k) for k in ("patient", "date", "start", "provider_id", "service_id")):
            raise ValueError("changed")
    except (ValueError, KeyError, TypeError):
        state.update({"pending_action": None, "stage": "cancellation_unavailable"})
        session_store.save(state)
        return _approval_response(state["session_id"], state, say(state, "Cancellation could not be verified. Please call the clinic.", "ما قدرت أتحقق من الإلغاء. تواصل مع العيادة."))
    result = await gateway.call_write("cancel_booking", pending["payload"])
    if result.get("ok") and result.get("booking_id") == booking["booking_id"] and result.get("status") == "cancelled":
        state.update({"pending_action": None, "stage": "booking_cancelled"})
        session_store.save(state)
        return _approval_response(state["session_id"], state, say(state, "Appointment cancelled. Reference: ", "تم إلغاء الموعد. المرجع: ") + result["booking_id"], result=result)
    if result.get("error", {}).get("code") in ("too_late_to_cancel", "already_cancelled"):
        state["pending_action"] = None
    session_store.save(state)
    return _approval_response(state["session_id"], state, say(state, "Cancellation was not confirmed. Please call the clinic.", "الإلغاء مش مؤكد. تواصل مع العيادة."))
