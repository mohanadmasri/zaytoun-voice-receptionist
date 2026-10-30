
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

    # ==================================================
    # تصنيف رسالة المريض الحالية
    #
    # intent: نوع طلب المستخدم.
    # safety: مستوى الخطورة الطبية.
    # injection_detected: هل توجد محاولة لتجاوز التعليمات؟
    #
    # هذه التصنيفات تخص الرسالة الحالية فقط.
    # ==================================================

    intent: Literal[
        "booking", "medical_question", "off_topic", "other"
    ]

    safety: Literal[
        "none", "emergency", "urgent_medical", "medical_advice"
    ]

    injection_detected: bool
    # معلومات أولية يستخرجها LLM من رسالة المريض
    service_text: str | None
        # اسم الطبيب الذي طلبه المريض في المحادثة.
    provider_text: str | None
        # طريقة اختيار الطبيب:
    # auto: نستفيد من التفضيل المسجل.
    # specific: المريض طلب طبيبا محددا.
    # any: المريض يقبل أي طبيب مناسب للخدمة.
    provider_choice: Literal["auto", "specific", "any"]
    date_text: str | None
    time_text: str | None

    # معلومات الموعد بعد معالجتها والتحقق منها
    service_id: str | None
    requested_date: str | None
    time_window: str | None

    # تذكير — نتيجة تحليل الوقت.
    # exact: ساعة محددة.
    # window: فترة زمنية.
    # missing: لم يحدد المريض وقتا.
    # ambiguous/unrecognized: نحتاج توضيحا.
    time_kind: Literal[
        "exact", "window", "missing",
        "ambiguous", "unrecognized"
    ]

    # الوقت المحدد، مثلا 16:30.
    requested_time: str | None

    # حدود الفترة الزمنية للبحث عن المواعيد.
    time_after: str | None
    time_before: str | None

    # معلومات المريض والطبيب
    
    # معلومات المريض والطبيب المستخدم للحجز الحالي.
    patient_name: str | None
    patient_phone: str | None
    provider_id: str | None

    # تذكير:
    # الطبيب المفضل في سجل المريض.
    # لا يعني بالضرورة أننا سنحجز عنده.
    preferred_provider_id: str | None

    # ==================================================
    # تذكير — ذاكرة المريض العائد
    #
    # patient_lookup_phone:
    # رقم الهاتف الذي بحثنا عنه بالفعل.
    #
    # patient_lookup_status:
    # نتيجة البحث عن المريض في نظام العيادة.
    #
    # نستخدمهما حتى لا نكرر استدعاء get_patient
    # عند كل رسالة في نفس المحادثة.
    # ==================================================

    patient_lookup_phone: str | None

    patient_lookup_status: Literal[
        "not_checked", "missing", "found", "new", "unavailable"
    ]

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
        "language": "unknown",        # التصنيف الأولي قبل وصول رسالة من المريض.
        "intent": "other",
        "safety": "none",
        "injection_detected": False,

        # لم يستخرج LLM معلومات الحجز بعد
        "service_text": None,
                # لم يطلب المريض طبيبا محددا بعد.
        "provider_text": None,
                # في بداية المحادثة لم يحدد المريض تفضيله.
        "provider_choice": "auto",
        "date_text": None,
        "time_text": None,

        # لم يتم تحديد الخدمة والتاريخ والوقت بشكل مؤكد
        "service_id": None,
        "requested_date": None,
        "time_window": None,

        # لم نحلل تفضيل الوقت بعد.
        "time_kind": "missing",
        "requested_time": None,
        "time_after": None,
        "time_before": None,

        # معلومات المريض غير معروفة بعد
        "patient_name": None,
        "patient_phone": None,
        "provider_id": None,
                # لا نعرف الطبيب المفضل قبل البحث عن المريض.
        "preferred_provider_id": None,
        # لم نبحث عن المريض في نظام العيادة بعد.
        "patient_lookup_phone": None,
        "patient_lookup_status": "not_checked",
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
