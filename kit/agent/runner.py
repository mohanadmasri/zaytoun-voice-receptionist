
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



# تنسق معالجة رسالة واحدة من المريض.
async def handle_message(
    session_id: str,
    message: str,
    gateway: ClinicGateway
) -> ChatResponse:

    # 1. استرجاع بيانات المحادثة السابقة
    state = session_store.get_or_create(session_id)

    # 2. وضع رسالة المريض الجديدة داخل الحالة
    state["current_message"] = message

    # 3. تشغيل LangGraph وتمرير Gateway
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
    }

    # إضافة عملية تنتظر الموافقة إذا كانت موجودة
    if updated_state.get("pending_action") is not None:
        response["pending_action"] = updated_state["pending_action"]

    return response
