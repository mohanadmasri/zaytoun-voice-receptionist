
# scheduler_node.py
# هذا الملف يربط Scheduler مع LangGraph.
# يأخذ Gateway من Runtime Context.
# لا ينفذ أي عملية حجز أو إلغاء.

from langgraph.runtime import Runtime

from agent.scheduler import schedule
from agent.state import AgentState
from core.contracts import ClinicGateway
from typing import TypedDict


# تعريف الأدوات الخارجية التي تحتاجها العقدة.
class SchedulerContext(TypedDict):
    gateway: ClinicGateway


# تشغيل Scheduler عندما تصبح بيانات الموعد جاهزة.
async def scheduler_node(
    state: AgentState,
    runtime: Runtime[SchedulerContext],
) -> dict:

    # نتأكد أن الطلب وصل إلى مرحلة البحث عن المواعيد.
    if state.get("stage") != "needs_slot_lookup":
        return {}

    # الحصول على Gateway الذي مرره LangGraph.
    gateway = runtime.context["gateway"]

    # تشغيل البحث باستخدام بيانات المريض الحالية.
    result = await schedule(state, gateway)

    # إعادة نتيجة البحث إلى الحالة المشتركة.
    return result
