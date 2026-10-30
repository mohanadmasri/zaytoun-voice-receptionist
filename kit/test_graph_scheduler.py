
# test_graph_scheduler.py
# هذا الملف يختبر محادثة كاملة مع LangGraph.
# يعرض المعلومات التي استخرجها Intake والتحقق من التاريخ.
# لا ينشئ أي حجز.

import asyncio
from uuid import uuid4

from agent.runner import handle_message
from core.session_store import session_store
from integrations.mcp_gateway import ClinicGateway


# تشغيل اختبار محادثة واحدة.
async def main():

    # إنشاء Gateway للاتصال بالعيادة.
    gateway = ClinicGateway()

    # إنشاء معرف جديد لتجنب استخدام بيانات اختبار قديم.
    session_id = f"scheduler-test-{uuid4().hex}"

    # رسالة المريض التي سيحللها Intake.
    message = (
        "I want a dental checkup on November 2, 2026 "
        "after 3 PM. Any available doctor is fine."
    )

    print("Patient:", message)
    print("-" * 50)

    # تشغيل المحادثة الكاملة.
    response = await handle_message(
        session_id=session_id,
        message=message,
        gateway=gateway,
    )

    # قراءة الحالة المحفوظة داخل نفس عملية Python.
    state = session_store.get_or_create(session_id)

    print("\n=== FINAL RESPONSE ===")
    print("Stage:", response.get("state"))
    print("Reply:", response.get("reply"))
    print("Tool calls:", response.get("tool_calls"))
    print("Pending action:", response.get("pending_action"))

    # عرض البيانات التي استخرجها النظام.
    print("\n=== AGENT STATE ===")
    print("date_text:", repr(state.get("date_text")))
    print("requested_date:", repr(state.get("requested_date")))
    print("service_text:", repr(state.get("service_text")))
    print("service_id:", repr(state.get("service_id")))
    print("time_text:", repr(state.get("time_text")))
    print("provider_choice:", repr(state.get("provider_choice")))
    print("provider_id:", repr(state.get("provider_id")))
    print("preferred_provider_id:", repr(state.get("preferred_provider_id")))

    # عرض عدد المواعيد إذا وصلنا إلى Scheduler.
    slots = state.get("proposed_slots") or []

    print("\n=== SCHEDULER ===")
    print("Slots found:", len(slots))

    for slot in slots:
        print(
            "Start:", slot.get("start"),
            "Provider:", slot.get("provider_id"),
        )


# تشغيل الاختبار.
if __name__ == "__main__":
    asyncio.run(main())
