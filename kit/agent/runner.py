
# ==================================================
# تذكير — runner.py
#
# هذا الملف يربط SessionStore مع LangGraph.
# handle_message تستقبل رسالة المريض و session_id.
# get_or_create تسترجع الحالة السابقة أو تنشئ واحدة.
# graph.ainvoke يعالج الرسالة ويحدث البيانات.
# save تحفظ الحالة الجديدة للمحادثة التالية.
# Gateway يمرر إلى Graph عبر Runtime Context.
# ==================================================

from agent.graph import graph
from agent.appointment_history import history_query, history_reply
from core.contracts import ChatResponse, ClinicGateway
from core.session_store import session_store
from agent.state import create_initial_state
import re
from time import perf_counter
from agent.presentation import display_date, display_time, provider_label, say
from agent.patient_identity import third_party_request
from agent.patient_names import declared_patient_name, patient_name_statement
from agent.times import is_appointment_time_change



# تنسق رسالة message داخل session_id باستخدام gateway؛ input_language لغة تسجيل مفحوصة أو None للنص.
# async/await تنتظر Graph دون حجب استقبال بقية الجلسات، ثم تحفظ الحالة لنفس الجلسة.
async def handle_message(
    session_id: str,
    message: str,
    gateway: ClinicGateway,
    input_language: str | None = None,
) -> ChatResponse:

    # 1. استرجاع بيانات المحادثة السابقة
    state = session_store.get_or_create(session_id)
    if query := history_query(message):
        language = input_language or ("ar" if re.search(r"[\u0600-\u06ff]", message) else "en")
        response: ChatResponse = {
            "session_id": session_id, "state": state.get("stage"), "language": language, "tool_calls": [],
            "reply": history_reply(state.get("session_appointments", []), query, language),
            "appointment_history": bool(state.get("session_appointments")),
        }
        if state.get("pending_action"):
            response["pending_action"] = state["pending_action"]
        return response
    if state.get("stage") == "booking_confirmed" and is_appointment_time_change(message):
        return {"session_id": session_id, "state": "booking_confirmed",
                "language": input_language or state.get("language", "en"), "tool_calls": [],
                "reply": say({"language": input_language or state.get("language")},
                             "Please contact the clinic to change the confirmed appointment.",
                             "تواصل مع العيادة لتغيير الموعد المؤكد، لو سمحت.")}
    # انتهاء طلب سابق لا يسمح بنقل هوية أو موعد إلى الطلب التالي؛ التحية أثناء طلب معلق لا تمسحه.
    if state.get("stage") in ("booking_confirmed", "booking_rejected", "booking_cancelled") or re.search(r"\b(?:start a new|new appointment|another appointment)\b|ابدأ من جديد|ابدا من جديد|موعد جديد", message, re.I):
        state = {**create_initial_state(session_id), "language": state.get("language", "unknown"),
                 "session_appointments": state.get("session_appointments", [])}

    # 2. وضع رسالة المريض الجديدة داخل الحالة
    state["current_message"] = message
    state["current_input_language"] = input_language

    # 3. تشغيل LangGraph وتمرير Gateway
    started = perf_counter()
    updated_state = await graph.ainvoke(
        state,
        context={"gateway": gateway}
    )

    # تبديل هوية موجودة بالصوت يحتاج توضيحًا صريحًا؛ التفريغ التلقائي ليس إثبات هوية.
    old_name = state.get("patient_name")
    new_name = updated_state.get("patient_name")
    explicit_name = declared_patient_name(message, old_name, new_name)
    incomplete_name = new_name is None and patient_name_statement(message, standalone=True) is not None
    if input_language and (old_name or state.get("patient_phone")) and (
        third_party_request(message)
        or (old_name and new_name != old_name and not incomplete_name and (not explicit_name or new_name != explicit_name))
        or (state.get("confirmed_phone") == state.get("patient_phone") and state.get("patient_phone") and updated_state.get("patient_phone") != state.get("patient_phone"))
    ):
        response = {"session_id": session_id, "state": state.get("stage"), "language": input_language,
                    "reply": say({"language": input_language}, "To change the patient, please type the actual patient's full name and phone to confirm. Your current request is unchanged.", "لتغيير المريض، اكتب اسم المريض الفعلي ورقم تلفونه للتأكيد. طلبك الحالي ما تغيّر."),
                    "tool_calls": updated_state.get("tool_calls", [])}
        if state.get("pending_action"):
            response["pending_action"] = state["pending_action"]
        return response
    # 4. حفظ الحالة المحدثة للمحادثة التالية
    selected_id = updated_state.get("selected_slot_id")
    previous_id = state.get("selected_slot_id")
    replacement = None
    if previous_id and selected_id and selected_id != previous_id:
        replacement = next((slot for slot in updated_state.get("proposed_slots", [])
                            if slot["slot_id"] == selected_id), None)
    if replacement:
        language = updated_state.get("language", "en")
        day = display_date(replacement["date"], language)
        clock = display_time(replacement["start"], language)
        provider = provider_label(replacement, updated_state)
        confirmation = say(updated_state, f"Your selected appointment is now {day} at {clock} with {provider}.",
                           f"موعدك المختار صار يوم {day} الساعة {clock} مع {provider}.")
        updated_state["reply"] = confirmation + "\n" + updated_state.get("reply", "")
    session_store.save(updated_state)

    # 5. تجهيز الرد حسب العقد المشترك مع FastAPI
    response: ChatResponse = {
        "session_id": session_id,
        "reply": updated_state.get("reply", ""),
        "tool_calls": updated_state.get("tool_calls", []),
        "state": updated_state.get("stage"),
        "language": updated_state.get("language", "en"),
        "timings": {"intake_ms": updated_state.get("intake_ms", 0), "graph_ms": round((perf_counter() - started) * 1000, 2)},
    }

    # إضافة عملية تنتظر الموافقة إذا كانت موجودة
    if updated_state.get("pending_action") is not None:
        response["pending_action"] = updated_state["pending_action"]
    if replacement:
        response["appointment_selection_changed"] = True

    return response
