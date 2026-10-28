
# ==================================================
# تذكير — graph.py
#
# يربط خطوات النظام باستخدام LangGraph.
# intake_node تستخرج المعلومات وتحدث AgentState.
# build_graph تحدد ترتيب تنفيذ العقد.
# هذه نسخة أولية تحتوي على Intake فقط.
# ==================================================

from langgraph.graph import StateGraph, START, END

from agent.state import AgentState
from agent.intake import extract_request, update_state_from_intake


# تنفذ Intake وتدمج المعلومات المستخرجة مع حالة المحادثة.
async def intake_node(state: AgentState) -> AgentState:

    # قراءة رسالة المريض الحالية
    message = state["current_message"]

    # إرسال الرسالة إلى LLM لاستخراج بيانات الحجز
    result = await extract_request(message)

    # دمج المعلومات المستخرجة مع البيانات السابقة
    updated_state = update_state_from_intake(state, result)

    # إضافة رسالة المريض إلى سجل المحادثة
    updated_state["messages"] = [
        *state.get("messages", []),
        {"role": "user", "content": message},
    ]

    return updated_state


# تنشئ مخطط LangGraph وتحدد بداية المسار ونهايته.
def build_graph():

    # إنشاء مخطط يستخدم AgentState كحالة مشتركة
    builder = StateGraph(AgentState)

    # إضافة عقدة Intake إلى المخطط
    builder.add_node("intake", intake_node)

    # تحديد ترتيب التنفيذ
    builder.add_edge(START, "intake")
    builder.add_edge("intake", END)

    # تحويل المخطط إلى نسخة قابلة للتنفيذ
    return builder.compile()


# إنشاء نسخة جاهزة للاستخدام من المخطط
graph = build_graph()
