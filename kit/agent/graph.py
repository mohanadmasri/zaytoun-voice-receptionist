
# ==================================================
# تذكير — graph.py
#
# ينظم LangGraph تنفيذ العقد باستخدام AgentState.
# intake_node تفهم رسالة المريض وتحدث الحالة.
# normalize_date_node تحصل على تاريخ العيادة عبر Gateway.
# choose_next تختار المسار المناسب حسب البيانات.
# build_graph تربط العقد وتحدد ترتيب تنفيذها.
# لا يوجد حجز فعلي في هذه المرحلة.
# ==================================================

from time import perf_counter
from typing import Literal, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime

from agent.state import AgentState
from agent.intake import extract_request, update_state_from_intake
from agent.dates import normalize_date
from core.contracts import ClinicGateway

# ==================================================
# تذكير — Runtime Context
#
# المشكلة: بعض Nodes تحتاج أدوات خارجية مثل ClinicGateway.
# Runtime Context يمرر هذه الأدوات دون تخزينها في AgentState.
#
# AgentState: يحتفظ بمعلومات المريض وحالة المحادثة.
# Runtime Context: يوفر للـNodes الوصول إلى Gateway.
# Gateway: يسترجع تاريخ العيادة عبر MCP باستخدام clinic_info.
# normalize_date: تحول "next Tuesday" إلى تاريخ محدد.
# requested_date: يحفظ التاريخ الناتج داخل AgentState.
#
# Runtime Context لا يترجم التواريخ ولا يخزن بيانات المريض.
# هو بديل عن تمرير Gateway داخل State أو ربطه عند إنشاء Graph.
# ==================================================

# يحدد الأدوات الخارجية التي يحتاجها Graph أثناء التشغيل.
class GraphContext(TypedDict):
    gateway: ClinicGateway


# تستدعي Intake لاستخراج معلومات المريض وتحديث الحالة.
async def intake_node(state: AgentState) -> AgentState:

    # قراءة الرسالة الحالية
    message = state["current_message"]

    # استخراج المعلومات باستخدام LLM
    result = await extract_request(message)

    # دمج المعلومات الجديدة مع السابقة
    updated_state = update_state_from_intake(state, result)

    # حفظ رسالة المريض في سجل المحادثة
    updated_state["messages"] = [
        *state.get("messages", []),
        {"role": "user", "content": message},
    ]

    # بدء سجل جديد لاستدعاءات الأدوات في هذه الجولة
    updated_state["tool_calls"] = []

    # إعادة ضبط المرحلة قبل معالجة الطلب الحالي
    if updated_state.get("language") != "unsupported":
        updated_state["stage"] = "intake"

    return updated_state


# تحصل على تاريخ العيادة وتحول تاريخ المريض إلى YYYY-MM-DD.
async def normalize_date_node(
    state: AgentState,
    runtime: Runtime[GraphContext]
) -> dict:

    # لا نعالج التاريخ عندما تكون اللغة غير مدعومة
    if state.get("language") == "unsupported":
        return {}

    # لا حاجة لاستدعاء العيادة إذا لم يذكر المريض تاريخًا
    date_text = state.get("date_text")
    if not date_text:
        return {}

    # الحصول على Gateway من Runtime Context
    gateway = runtime.context["gateway"]

    # قياس زمن استدعاء أداة العيادة
    start = perf_counter()

    try:
        clinic = await gateway.call_read("clinic_info", {})
    except Exception:
        clinic = {"ok": False}

    duration_ms = round((perf_counter() - start) * 1000, 2)

    # التحقق من نجاح الأداة ووجود تاريخ صحيح
    success = (
        isinstance(clinic, dict)
        and clinic.get("ok") is True
        and isinstance(clinic.get("today"), str)
    )

    # تسجيل استدعاء الأداة في الحالة
    calls = [
        *state.get("tool_calls", []),
        {
            "tool": "clinic_info",
            "ok": success,
            "ms": duration_ms,
        },
    ]

    # عند فشل الاتصال لا نخمن تاريخ العيادة
    if not success:
        return {
            "requested_date": None,
            "stage": "clinic_unavailable",
            "tool_calls": calls,
        }

    # تحويل التاريخ باستخدام الدالة الموجودة في dates.py
    try:
        resolved_date = normalize_date(
            date_text,
            clinic["today"]
        )
    except ValueError:
        resolved_date = None

    # تخزين التاريخ النهائي داخل AgentState
    return {
        "requested_date": resolved_date,
        "tool_calls": calls,
    }


# تفحص AgentState وتحدد اتجاه التنفيذ.
def choose_next(
    state: AgentState
) -> Literal["unsupported", "clinic_error", "clarify", "ready"]:

    # رفض اللغة غير المدعومة
    if state.get("language") == "unsupported":
        return "unsupported"

    # عدم الاستمرار عند فشل معلومات العيادة
    if state.get("stage") == "clinic_unavailable":
        return "clinic_error"

    # طلب معلومات ناقصة أو تاريخ لم نفهمه
    if not state.get("service_text") or not state.get("requested_date"):
        return "clarify"

    # الخدمة والتاريخ موجودان، ويمكن استكمال التحقق
    return "ready"


# تسأل عن أول معلومة ناقصة.
def clarify_node(state: AgentState) -> dict:

    is_arabic = state.get("language") == "ar"

    if not state.get("service_text"):
        reply = (
            "ما نوع الموعد الذي ترغب بحجزه؟"
            if is_arabic
            else "What type of appointment would you like?"
        )
    else:
        reply = (
            "في أي يوم ترغب بحجز الموعد؟"
            if is_arabic
            else "Which day would you like the appointment?"
        )

    return {
        "reply": reply,
        "stage": "awaiting_details",
    }


# تحدد أن الخدمة والتاريخ جاهزان للمرحلة التالية.
def ready_node(state: AgentState) -> dict:

    appointment_date = state["requested_date"]

    reply = (
        f"فهمت طلبك ليوم {appointment_date}. سأتحقق من الخدمة والمواعيد المتاحة."
        if state.get("language") == "ar"
        else f"I have your request for {appointment_date}. "
             "The service and available slots still need verification."
    )

    return {
        "reply": reply,
        "stage": "needs_service_mapping",
    }


# تعالج حالة اللغة غير المدعومة.
def unsupported_node(state: AgentState) -> dict:

    return {
        "reply": (
            "This clinic supports English and Arabic only. "
            "هذه العيادة تدعم العربية والإنجليزية فقط."
        ),
        "stage": "unsupported_language",
    }


# تعالج فشل الاتصال بمعلومات العيادة.
def clinic_error_node(state: AgentState) -> dict:

    return {
        "reply": (
            "تعذر التحقق من تاريخ العيادة حاليًا. يرجى المحاولة مجددًا."
            if state.get("language") == "ar"
            else "I cannot verify the clinic date right now. "
                 "Please try again."
        ),
        "stage": "clinic_unavailable",
    }


# تنشئ العقد والمسارات المشروطة في LangGraph.
def build_graph():

    # إنشاء Graph مع الحالة وواجهة الأدوات الخارجية
    builder = StateGraph(
        AgentState,
        context_schema=GraphContext,
    )

    # تسجيل العقد
    builder.add_node("intake", intake_node)
    builder.add_node("normalize_date", normalize_date_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("ready", ready_node)
    builder.add_node("unsupported", unsupported_node)
    builder.add_node("clinic_error", clinic_error_node)

    # البداية: فهم الرسالة ثم معالجة التاريخ
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "normalize_date")

    # توجيه التنفيذ حسب الحالة بعد معالجة التاريخ
    builder.add_conditional_edges(
        "normalize_date",
        choose_next,
        {
            "clarify": "clarify",
            "ready": "ready",
            "unsupported": "unsupported",
            "clinic_error": "clinic_error",
        },
    )

    # تنتهي الجولة بعد العقدة المختارة
    builder.add_edge("clarify", END)
    builder.add_edge("ready", END)
    builder.add_edge("unsupported", END)
    builder.add_edge("clinic_error", END)

    # تجهيز Graph للتشغيل
    return builder.compile()


# نسخة جاهزة للاستخدام
graph = build_graph()
