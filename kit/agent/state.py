
# ============================================================
# تذكير للشرح — state.py:
#
# AgentState: يحدد البيانات المشتركة بين Intake وScheduler داخل LangGraph.
# يحفظ معلومات المريض والموعد ومرحلة المحادثة.
# نميز بين المعلومات الأولية المستخرجة من LLM والمعلومات المؤكدة.
# create_initial_state: تنشئ حالة جديدة بقيم ابتدائية فارغة.
# حفظ الحالة بين الرسائل سيكون مسؤولية SessionStore لاحقًا.
# ============================================================


from typing import Any, Literal, TypedDict

from core.contracts import PendingAction, ToolCall


# يحدد شكل الحالة المشتركة التي تنتقل بين خطوات LangGraph.
class AgentState(TypedDict, total=False):

    # معرّف المحادثة واللغة المستخدمة
    session_id: str
    language: Literal["en", "ar", "unknown", "unsupported"]

    # معلومات أولية يستخرجها LLM من رسالة المريض
    service_text: str | None
    date_text: str | None
    time_text: str | None

    # معلومات الموعد بعد معالجتها والتحقق منها
    service_id: str | None
    requested_date: str | None
    time_window: str | None

    # معلومات المريض والطبيب
    patient_name: str | None
    patient_phone: str | None
    provider_id: str | None

    # المواعيد المتاحة والحجز الذي ينتظر الموافقة
    proposed_slots: list[dict[str, Any]]
    selected_slot_id: str | None
    pending_action: PendingAction | None

    # سجل المحادثة والرد واستدعاءات الأدوات
    messages: list[dict[str, str]]
    current_message: str
    reply: str
    tool_calls: list[ToolCall]

    # المرحلة الحالية من سير العمل
    stage: str


# تنشئ حالة محادثة جديدة وتضع قيمًا ابتدائية لجميع الحقول.
def create_initial_state(session_id: str) -> AgentState:
    return {
        "session_id": session_id,
        "language": "unknown",

        # لم يستخرج LLM معلومات الحجز بعد
        "service_text": None,
        "date_text": None,
        "time_text": None,

        # لم يتم تحديد الخدمة والتاريخ والوقت بشكل مؤكد
        "service_id": None,
        "requested_date": None,
        "time_window": None,

        # معلومات المريض غير معروفة بعد
        "patient_name": None,
        "patient_phone": None,
        "provider_id": None,

        # لا توجد مواعيد مقترحة أو عمليات تنتظر الموافقة
        "proposed_slots": [],
        "selected_slot_id": None,
        "pending_action": None,

        # المحادثة تبدأ دون رسائل أو نتائج أدوات
        "messages": [],
        "current_message": "",
        "reply": "",
        "tool_calls": [],

        # تبدأ المحادثة عند Intake Agent
        "stage": "intake",
    }
