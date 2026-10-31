
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
from core.contracts import ChatResponse, ClinicGateway
from core.session_store import session_store
from agent.state import create_initial_state
import re
from time import perf_counter



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
    # انتهاء طلب سابق لا يسمح بنقل هوية أو موعد إلى الطلب التالي؛ التحية أثناء طلب معلق لا تمسحه.
    if state.get("stage") in ("booking_confirmed", "booking_rejected") or re.search(r"\b(?:start a new|new appointment|another appointment)\b|ابدأ من جديد|ابدا من جديد|موعد جديد", message, re.I):
        state = {**create_initial_state(session_id), "language": state.get("language", "unknown")}

    # 2. وضع رسالة المريض الجديدة داخل الحالة
    state["current_message"] = message
    state["current_input_language"] = input_language

    # 3. تشغيل LangGraph وتمرير Gateway
    started = perf_counter()
    updated_state = await graph.ainvoke(
        state,
        context={"gateway": gateway}
    )

    # 4. حفظ الحالة المحدثة للمحادثة التالية
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

    return response
