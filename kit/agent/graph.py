# ==================================================
# تذكير — graph.py
#
# ينظم LangGraph تنفيذ العقد باستخدام AgentState.
# intake_node تفهم رسالة المريض وتحدث الحالة.
# normalize_date_node تحصل على تاريخ العيادة عبر Gateway.
# normalize_time_node تحول نص الوقت إلى حدود بحث باستخدام Python فقط.
# choose_next تختار المسار المناسب حسب البيانات.
# build_graph تربط العقد وتحدد ترتيب تنفيذها.
# لا يوجد حجز فعلي في هذه المرحلة.
# المدخل حالة المحادثة والرسالة؛ المخرج رد وحالة وموافقة معلقة عند الجاهزية.
# يستخدم LLM للفهم وMCP للقراءة؛ متابعة الاختيار لا تستدعي أدوات العيادة.
# ==================================================
from agent.guardrails import check_guardrails
from time import perf_counter
from typing import Literal, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime

from agent.state import AgentState
from agent.intake import extract_request, update_state_from_intake
from agent.dates import normalize_date
from agent.times import normalize_time
from core.contracts import ClinicGateway
from agent.service_mapping import match_service
from agent.patient_memory import lookup_returning_patient
from agent.service_mapping import match_provider
from agent.scheduler_node import scheduler_node
from agent.slot_selection import select_slot, extract_time, find_slots_by_id
from agent.slot_handoff import slot_matches_request
from actions.approval import prepare_booking_action
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
    # ==================================================
    # تذكير — فحص الحماية المستقل
    #
    # 1. نفحص رسالة المريض باستخدام قواعد Python.
    # 2. إذا اكتشفنا طوارئ، نعطيها الأولوية.
    # 3. إذا اكتشفنا تلاعبا، نسجل ذلك في AgentState.
    # 4. لا نسمح للفحص الجديد بإلغاء تحذير أخطر من LLM.
    #
    # الهدف: إضافة طبقة حماية لا تعتمد على LLM وحده.
    # ==================================================

    decision = check_guardrails(
        message,
        updated_state.get("language", "unknown")
    )

    if decision["kind"] == "emergency":
        updated_state["safety"] = "emergency"

    elif decision["kind"] == "injection":
        updated_state["injection_detected"] = True

    elif decision["kind"] == "medical_advice":
        if updated_state.get("safety") == "none":
            updated_state["safety"] = "medical_advice"
    # حفظ رسالة المريض في سجل المحادثة
    updated_state["messages"] = [
        *state.get("messages", []),
        {"role": "user", "content": message},
    ]

    # بدء سجل جديد لاستدعاءات الأدوات في هذه الجولة
    updated_state["tool_calls"] = []

    return updated_state


# تستقبل حالة المحادثة وتعيد حقول الوقت المنظمة للبحث والتحقق من النتائج.
# تُنفذ في مسار البحث فقط؛ تعيد missing عند غياب الوقت وتميز الغامض دون تخمين.
# لا تستخدم LLM أو MCP أو API، ولا تغيّر مرحلة خطأ العيادة أو تنشئ حجزًا.
def normalize_time_node(state: AgentState) -> dict:
    preference = normalize_time(state.get("time_text"))
    return {
        "time_kind": preference["kind"],
        "requested_time": preference["requested_time"],
        "time_after": preference["after"],
        "time_before": preference["before"],
    }


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

# ==================================================
# تذكير — service_mapping_node
#
# 1. تستقبل اسم الخدمة المستخرج من كلام المريض.
#
# 2. تستدعي list_services عبر ClinicGateway.
#
# 3. تستخدم match_service لمقارنة أسماء الخدمات
#    بالعربية والإنجليزية.
#
# 4. تحفظ service_id الرسمي عند وجود تطابق واضح.
#
# 5. عند عدم وجود تطابق تعيد None ولا تخمن الخدمة.
#
# 6. تسجل نتيجة استدعاء الأداة ومدة تنفيذها.
#
# لا تحجز موعدا ولا تستدعي أدوات الكتابة.
# ==================================================

async def service_mapping_node(
    state: AgentState,
    runtime: Runtime[GraphContext]
) -> dict:

    # لا نبحث عن خدمة إذا كانت اللغة غير مدعومة.
    if state.get("language") == "unsupported":
        return {}

    # لا نتابع إذا فشل التحقق من تاريخ العيادة.
    if state.get("stage") == "clinic_unavailable":
        return {}

    # لا نبحث إذا لم يذكر المريض نوع الخدمة.
    if not state.get("service_text"):
        return {}

    # إذا عرفنا الخدمة الرسمية سابقا، نحتفظ بها.
    if state.get("service_id"):
        return {}

    gateway = runtime.context["gateway"]

    start = perf_counter()

    # الحصول على الخدمات الرسمية من العيادة.
    try:
        result = await gateway.call_read(
            "list_services",
            {}
        )
    except Exception:
        result = {"ok": False}

    duration_ms = round(
        (perf_counter() - start) * 1000,
        2
    )

    success = (
        isinstance(result, dict)
        and result.get("ok") is True
        and isinstance(result.get("services"), list)
    )

    calls = [
        *state.get("tool_calls", []),
        {
            "tool": "list_services",
            "ok": success,
            "ms": duration_ms,
        },
    ]

    # لا نستعمل قائمة خدمات إذا فشل الاتصال.
    if not success:
        return {
            "stage": "services_unavailable",
            "tool_calls": calls,
        }

    # مقارنة كلام المريض بالخدمات الرسمية.
    service_id = match_service(
        state["service_text"],
        result["services"]
    )

    return {
        "service_id": service_id,
        "tool_calls": calls,
    }

# ==================================================
# تذكير — route_after_intake
#
# 1. تستقبل AgentState بعد انتهاء Intake.
# 2. تفحص التصنيف الذي أعاده LLM.
# 3. تعطي الأولوية للحالات الطبية الطارئة.
# 4. تمنع استمرار الطلبات غير المناسبة إلى الحجز.
# 5. تسمح للطلبات العادية بمتابعة التنفيذ.
#
# هذه الدالة تختار المسار فقط ولا تنفذ أدوات.
# ==================================================

def route_after_intake(
    state: AgentState
) -> Literal["normal", "safety", "unsupported", "slot_followup"]:

    # الطوارئ لها الأولوية القصوى.
    if state.get("safety") == "emergency":
        return "safety"

    # رفض اللغات غير المدعومة.
    if state.get("language") == "unsupported":
        return "unsupported"

    # محاولة تجاوز قواعد النظام.
    if state.get("injection_detected"):
        return "safety"

    # الحالات الطبية التي لا يجب التعامل معها كحجز عادي.
    if state.get("safety") in (
        "urgent_medical",
        "medical_advice",
    ):
        return "safety"

    # الأسئلة الطبية أو الخارجة عن اختصاص العيادة.
    if state.get("intent") in (
        "medical_question",
        "off_topic",
    ):
        return "safety"

    # نوجّه المتابعة بعد فحوص السلامة، بشرط بقاء المواعيد صالحة.
    if state.get("proposed_slots") and state.get("stage") in (
        "slots_proposed", "awaiting_slot_selection", "slot_selected",
        "awaiting_patient_details", "awaiting_approval",
    ):
        return "slot_followup"

    # السماح بمتابعة المسار العادي.
    return "normal"


# تستقبل حالة المتابعة وتعيد الاختيار أو طلب البيانات أو الموافقة المعلقة.
# تتحقق من انتماء الاختيار للقائمة ومطابقته للطلب؛ لا تستخدم أدوات خارجية.
# الاسم والهاتف يكملان الاختيار السابق؛ ذكر وقت أو معرّف يعيد فحص الاختيار.
def slot_followup_node(state: AgentState) -> dict:
    updates = {}
    message = state.get("current_message", "")
    choosing = bool(
        extract_time(message) is not None
        or find_slots_by_id(state.get("proposed_slots", []), message)
    )
    if choosing or not state.get("selected_slot_id"):
        updates = select_slot(state, message)
        if updates.get("stage") != "slot_selected":
            return {**updates, "pending_action": None}

    # دمج محلي لأن LangGraph يطبّق تحديثات العقدة بعد إرجاعها فقط.
    selected_state = {**state, **updates}
    selected = next((slot for slot in selected_state.get("proposed_slots", [])
                     if slot.get("slot_id") == selected_state.get("selected_slot_id")), None)
    if selected is None or not slot_matches_request(selected_state, selected):
        return {"stage": "awaiting_slot_selection", "selected_slot_id": None,
                "pending_action": None, "reply": "Please choose a matching displayed slot."}

    if not selected_state.get("patient_name") or not selected_state.get("patient_phone"):
        return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                "reply": "Please provide your full name and phone number."}

    try:
        pending = prepare_booking_action(selected_state)
    except ValueError:
        # لا نتجاوز تحقق الموافقة، بما فيه شرط ولي الأمر لمسار الأطفال.
        return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                "reply": "Booking details need verification before approval can be prepared."}
    return {**updates, "stage": "awaiting_approval", "pending_action": pending,
            "reply": f"Please review and explicitly approve: {pending['summary']}"}

# ==================================================
# تذكير — safety_node
#
# 1. تستقبل حالة المحادثة بعد تصنيف الرسالة.
# 2. تختار الرد المناسب حسب نوع المشكلة.
# 3. تعطي الأولوية للطوارئ الطبية.
# 4. تمنع متابعة الحجز في هذه الجولة.
# 5. تلغي أي طلب حجز سابق ينتظر الموافقة.
#
# لا تستخدم LLM أو MCP ولا تنشئ حجوزات.
# ==================================================

def safety_node(state: AgentState) -> dict:

    # تحديد لغة الرد.
    is_arabic = state.get("language") == "ar"

    # الحالة الأولى: طوارئ طبية.
    if state.get("safety") == "emergency":
        stage = "emergency"
        reply = (
            "قد تكون هذه حالة طارئة. اتصل على 101 الآن "
            "أو توجه إلى أقرب قسم طوارئ."
            if is_arabic
            else "This may be a medical emergency. "
                 "Call 101 now or go to the nearest emergency department."
        )

    # الحالة الثانية: محاولة تجاوز تعليمات النظام.
    elif state.get("injection_detected"):
        stage = "blocked_injection"
        reply = (
            "لا يمكن تجاوز قواعد الحجز والموافقة. "
            "يمكنني مساعدتك في طلب موعد واحد بطريقة صحيحة."
            if is_arabic
            else "Booking and approval rules cannot be bypassed. "
                 "I can help you request one appointment safely."
        )

    # الحالة الثالثة: مشكلة قد تحتاج تقييما طبيا عاجلا.
    elif state.get("safety") == "urgent_medical":
        stage = "urgent_medical"
        reply = (
            "يرجى طلب تقييم طبي عاجل. لا أستطيع تشخيص حالتك."
            if is_arabic
            else "Please seek prompt medical evaluation. "
                 "I cannot diagnose your condition."
        )

    # الحالة الرابعة: طلب تشخيص أو دواء.
    elif state.get("safety") == "medical_advice":
        stage = "medical_advice"
        reply = (
            "لا أستطيع تقديم تشخيص أو نصيحة دوائية، "
            "لكن يمكنني مساعدتك في حجز استشارة."
            if is_arabic
            else "I cannot provide a diagnosis or medication advice, "
                 "but I can help you book a consultation."
        )

    # الحالة الخامسة: سؤال خارج اختصاص العيادة.
    elif state.get("intent") == "off_topic":
        stage = "off_topic"
        reply = (
            "أنا مساعد استقبال للعيادة. "
            "يمكنني مساعدتك في مواعيد العيادة."
            if is_arabic
            else "I am a clinic receptionist assistant. "
                 "I can help with clinic appointments."
        )

    # أي سؤال طبي آخر لا يناسب الحجز المباشر.
    else:
        stage = "medical_question"
        reply = (
            "يمكنني مساعدتك في حجز استشارة طبية."
            if is_arabic
            else "I can help you book a medical consultation."
        )

    # إعادة الرد مع إلغاء أي عملية حجز معلقة.
    return {
        "reply": reply,
        "stage": stage,
        "pending_action": None,
        "selected_slot_id": None,
        "proposed_slots": [],
    }

# ==================================================
# تذكير — patient_lookup_node
#
# 1. نفحص هل المريض قدم رقم هاتف.
# 2. نتجنب البحث المتكرر عن الرقم نفسه.
# 3. نستدعي get_patient عبر patient_memory.py.
# 4. إذا وجدنا المريض، نسترجع اسمه وتفضيل طبيبه.
# 5. نحفظ نتيجة البحث داخل AgentState.
#
# لا ننشئ حجوزات ولا نعدل ملفات المرضى.
# ==================================================


# ==================================================
# تذكير — patient_lookup_node
#
# 1. تبحث عن المريض باستخدام رقم هاتفه.
# 2. تتجنب البحث المتكرر عن المريض نفسه.
# 3. تسترجع اسمه وطبيبه المفضل.
# 4. تحفظ الطبيب المفضل بشكل منفصل عن طبيب الحجز.
#
# provider_choice:
# auto     → استخدام الطبيب المفضل عند الإمكان.
# specific → احترام الطبيب الذي طلبه المريض.
# any      → السماح بالبحث عند جميع الأطباء.
#
# لا تنشئ هذه الدالة أي حجز.
# ==================================================

async def patient_lookup_node(
    state: AgentState,
    runtime: Runtime[GraphContext]
) -> dict:

    # قراءة رقم هاتف المريض.
    phone = state.get("patient_phone")

    # لا يمكن البحث دون رقم هاتف.
    if not phone:
        return {"patient_lookup_status": "missing"}

    phone = phone.strip()

    # تجنب البحث مرة أخرى إذا كان الرقم نفسه
    # وقد عرفنا بالفعل أن المريض موجود أو جديد.
    if (
        state.get("patient_lookup_phone") == phone
        and state.get("patient_lookup_status") in ("found", "new")
    ):
        return {}

    # الحصول على Gateway من Runtime Context.
    gateway = runtime.context["gateway"]

    # قياس زمن البحث.
    start = perf_counter()

    # البحث عن المريض عبر الدالة التي أنشأناها.
    result = await lookup_returning_patient(gateway, phone)

    duration_ms = round((perf_counter() - start) * 1000, 2)

    # حفظ نتيجة البحث وتسجيل استدعاء الأداة.
    updates = {
        "patient_lookup_phone": phone,
        "patient_lookup_status": result["status"],
        "tool_calls": [
            *state.get("tool_calls", []),
            {
                "tool": "get_patient",
                "ok": result["status"] == "found",
                "ms": duration_ms,
            },
        ],
    }

    # إذا كان المريض معروفا نستفيد من بياناته.
    if result["status"] == "found":

        # لا نستبدل اسما قدمه المستخدم بنفسه.
        if not state.get("patient_name") and result.get("name"):
            updates["patient_name"] = result["name"]

        # ==========================================
        # التعديل الجديد: فصل تفضيل الطبيب عن الحجز.
        #
        # preferred_provider_id:
        # الطبيب المفضل المحفوظ في ملف المريض.
        #
        # provider_id:
        # الطبيب الذي سنستخدمه للحجز الحالي.
        # ==========================================

        preferred_id = result.get("preferred_provider_id")

        # نحفظ الطبيب المفضل حتى لو قبل المريض أي طبيب.
        updates["preferred_provider_id"] = preferred_id

        # معرفة طريقة اختيار المريض للطبيب.
        provider_choice = state.get("provider_choice", "auto")

        # ==========================================
        # نستخدم الطبيب المفضل تلقائيا فقط إذا:
        #
        # 1. المريض لم يطلب طبيبا محددا.
        # 2. المريض لم يقل إن أي طبيب مناسب.
        # 3. لا يوجد طبيب مختار للحجز الحالي.
        #
        # لاحقا سنتحقق أن الطبيب يقدم الخدمة
        # المطلوبة داخل provider_mapping_node.
        # ==========================================

        if (
            provider_choice == "auto"
            and not state.get("provider_text")
            and not state.get("provider_id")
            and preferred_id
        ):
            updates["provider_id"] = preferred_id

    return updates

# ==================================================
# تذكير — provider_mapping_node
#
# الهدف: التحقق من الطبيب الذي سيُستخدم في الحجز.
#
# 1. نفحص هل توجد خدمة وطبيب مطلوب أو مفضل.
# 2. نسترجع قائمة الأطباء الرسمية من العيادة.
# 3. إذا طلب المريض طبيبا صراحة، نطابق اسمه.
# 4. إذا لم يطلب طبيبا، نتحقق من طبيبه المفضل.
# 5. نتأكد أن الطبيب يقدم الخدمة المطلوبة.
# 6. لا نخمن الطبيب عند فشل المطابقة.
#
# لا تنشئ هذه العقدة أي حجوزات.
# ==================================================

async def provider_mapping_node(
    state: AgentState,
    runtime: Runtime[GraphContext]
) -> dict:

    # قراءة معلومات الخدمة والطبيب.
    service_id = state.get("service_id")
    provider_text = state.get("provider_text")
    preferred_id = state.get("provider_id")

    # المريض صرح أنه يقبل أي طبيب.
    # لا نقيّد البحث بطبيب محدد.
    if state.get("provider_choice") == "any":
        return {"provider_id": None}
    
    # لا نتحقق من الطبيب قبل معرفة الخدمة.
    if not service_id:
        return {}

    # لا داعي لاستدعاء العيادة إذا لم يحدد
    # المريض طبيبا ولم يكن له طبيب مفضل.
    if not provider_text and not preferred_id:
        return {}

    # استرجاع بيانات الأطباء الرسمية.
    gateway = runtime.context["gateway"]

    start = perf_counter()

    try:
        response = await gateway.call_read(
            "clinic_info", {}
        )
    except Exception:
        response = {"ok": False}

    duration_ms = round(
        (perf_counter() - start) * 1000, 2
    )

    success = (
        isinstance(response, dict)
        and response.get("ok") is True
        and isinstance(response.get("providers"), list)
    )

    # تسجيل استدعاء أداة العيادة.
    calls = [
        *state.get("tool_calls", []),
        {
            "tool": "clinic_info",
            "ok": success,
            "ms": duration_ms,
        },
    ]

    # إذا فشل الاتصال، لا نخمن هوية الطبيب.
    if not success:
        return {
            "provider_id": None,
            "stage": "provider_check_failed",
            "tool_calls": calls,
        }

    providers = response["providers"]

    # الحالة الأولى: المريض طلب طبيبا صراحة.
    if provider_text:

        provider_id = match_provider(
            provider_text,
            providers,
            service_id
        )

        if provider_id is None:
            return {
                "provider_id": None,
                "stage": "provider_unverified",
                "tool_calls": calls,
            }

        return {
            "provider_id": provider_id,
            "stage": "provider_verified",
            "tool_calls": calls,
        }

    # الحالة الثانية: استخدام الطبيب المفضل،
    # بشرط أن يقدم الخدمة الحالية.
    valid_preference = any(
        provider.get("id") == preferred_id
        and service_id in provider.get("service_ids", [])
        for provider in providers
        if isinstance(provider, dict)
    )

    return {
        "provider_id": preferred_id if valid_preference else None,
        "tool_calls": calls,
    }


# ==================================================
# تذكير — choose_next
#
# تفحص AgentState وتحدد اتجاه التنفيذ.
#
# 1. رفض اللغات غير المدعومة.
# 2. إيقاف التنفيذ عند فشل أدوات العيادة.
# 3. طلب توضيح إذا تعذر التحقق من الطبيب.
# 4. طلب معلومات الحجز الناقصة.
# 5. السماح بالمتابعة إذا اكتملت البيانات.
# ==================================================

def choose_next(
    state: AgentState
) -> Literal[
    "unsupported",
    "clinic_error",
    "clarify",
    "clarify_provider",
    "ready",
]:

    # 1. رفض اللغة غير المدعومة.
    if state.get("language") == "unsupported":
        return "unsupported"

    # 2. عدم الاستمرار عند فشل معلومات العيادة
    # أو فشل التحقق من بيانات الأطباء.
    if state.get("stage") in (
        "clinic_unavailable",
        "services_unavailable",
        "provider_check_failed",
    ):
        return "clinic_error"

    # 3. المريض طلب طبيبا، لكننا لم نتمكن
    # من التحقق من اسمه أو ملاءمته للخدمة.
    if state.get("stage") == "provider_unverified":
        return "clarify_provider"

    # 4. طلب المعلومات الناقصة أو التاريخ غير المفهوم.
    # لا نتابع دون خدمة رسمية وتاريخ واضح.
    if (
        not state.get("service_text")
        or not state.get("requested_date")
        or not state.get("service_id")
    ):
        return "clarify"

    # 5. الخدمة والتاريخ موجودان، ويمكن استكمال الحجز.
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
    elif not state.get("requested_date"):
        reply = (
            "في أي يوم ترغب بحجز الموعد؟"
            if is_arabic
            else "Which day would you like the appointment?"
        )
    else:
        # إذا كان التاريخ معروفا لكن الخدمة غير محددة رسميا.
        reply = (
            "لم أتمكن من تحديد الخدمة المطلوبة. هل يمكنك توضيح نوع الموعد؟"
            if is_arabic
            else "I could not identify the service. "
                 "Could you clarify which appointment you need?"
        )

    return {
        "reply": reply,
        "stage": "awaiting_details",
    }


# تحدد أن الخدمة والتاريخ جاهزان للمرحلة التالية.
def ready_node(state: AgentState) -> dict:

    appointment_date = state["requested_date"]

    reply = (
        f"فهمت طلبك ليوم {appointment_date}. تحققت من الخدمة وسأبحث عن المواعيد المتاحة."
        if state.get("language") == "ar"
        else f"I have your request for {appointment_date}. "
             "The service is verified; available slots still need to be checked."
    )

    return {
        "reply": reply,
        "stage": "needs_slot_lookup",
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
            "تعذر التحقق من معلومات العيادة حاليًا. يرجى المحاولة مجددًا."
            if state.get("language") == "ar"
            else "I cannot verify the clinic information right now. "
                 "Please try again."
        ),
        "stage": state.get("stage", "clinic_unavailable"),
    }

# ==================================================
# تذكير — clarify_provider_node
#
# نستخدمها عندما يطلب المريض طبيبا
# لم نتمكن من مطابقته مع بيانات العيادة.
#
# نسأل عن الطبيب ولا نخمن بديلا.
# ==================================================

def clarify_provider_node(state: AgentState) -> dict:

    if state.get("language") == "ar":
        reply = (
            "لم أتمكن من التحقق من الطبيب الذي طلبته "
            "لهذه الخدمة. هل يمكنك توضيح اسم الطبيب؟"
        )
    else:
        reply = (
            "I couldn't verify the requested provider "
            "for this service. Could you clarify the "
            "provider's name?"
        )

    return {
        "reply": reply,
        "stage": "awaiting_provider_clarification",
    }
# تنشئ العقد والمسارات المشروطة في LangGraph.

# ==================================================
# تذكير — build_graph
#
# هذه الدالة تربط عقد LangGraph وتحدد ترتيب التنفيذ.
#
# 1. تبدأ بفهم رسالة المريض.
# 2. توجه الرسائل الخطرة إلى safety.
# 3. تبحث عن المريض إذا كان الطلب عاديا.
# 4. تعالج التاريخ والوقت والخدمة والطبيب بالترتيب.
# 5. تختار الرد المناسب بعد اكتمال المعالجة.
#
# تسجيل Node وحده لا يعني أنها ستُنفذ.
# يجب ربطها عبر add_edge أو add_conditional_edges.
# ==================================================

def build_graph():

    # إنشاء Graph باستخدام AgentState وRuntime Context.
    builder = StateGraph(
        AgentState,
        context_schema=GraphContext,
    )

    # ==============================================
    # أولاً: تسجيل جميع العقد
    # ==============================================

    builder.add_node("intake", intake_node)
    builder.add_node("slot_followup", slot_followup_node)

    builder.add_node("patient_lookup", patient_lookup_node)

    builder.add_node("safety", safety_node)

    builder.add_node("normalize_date", normalize_date_node)
    builder.add_node("normalize_time", normalize_time_node)

    builder.add_node("service_mapping", service_mapping_node)

    builder.add_node("provider_mapping", provider_mapping_node)

    builder.add_node("clarify", clarify_node)

    builder.add_node("clarify_provider", clarify_provider_node)

    builder.add_node("ready", ready_node)

    # تسجيل عقدة البحث عن المواعيد بعد اكتمال بيانات الطلب.
    builder.add_node("scheduler", scheduler_node)

    builder.add_node("unsupported", unsupported_node)

    builder.add_node("clinic_error", clinic_error_node)

    # ==============================================
    # ثانياً: نقطة بداية المحادثة
    # ==============================================

    # كل رسالة تبدأ من Intake.
    builder.add_edge(START, "intake")

    # ==============================================
    # ثالثاً: توجيه الرسالة بعد Intake
    # ==============================================

    # إذا كان الطلب عاديا، نبحث عن المريض.
    # إذا كان خطرا، ننتقل إلى safety.
    # إذا كانت اللغة غير مدعومة، نرفضها.
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        {
            "normal": "patient_lookup",
            "slot_followup": "slot_followup",
            "safety": "safety",
            "unsupported": "unsupported",
        },
    )

    # ==============================================
    # رابعاً: مسار معالجة طلب الحجز
    # ==============================================

    # بعد البحث عن المريض نكمل معالجة التاريخ.
    builder.add_edge(
        "patient_lookup",
        "normalize_date",
    )

    # بعد التاريخ نحوّل الوقت إلى شروط البحث قبل تحديد الخدمة.
    builder.add_edge(
        "normalize_date",
        "normalize_time",
    )
    builder.add_edge(
        "normalize_time",
        "service_mapping",
    )

    # بعد الخدمة نتحقق من الطبيب المطلوب.
    builder.add_edge(
        "service_mapping",
        "provider_mapping",
    )

    # ==============================================
    # خامساً: تحديد الخطوة التالية
    # ==============================================

    # choose_next تقرر هل نحتاج توضيحا،
    # أم نستطيع المتابعة، أم حدث خطأ.
    builder.add_conditional_edges(
        "provider_mapping",
        choose_next,
        {
            "clarify": "clarify",
            "clarify_provider": "clarify_provider",
            "ready": "ready",
            "unsupported": "unsupported",
            "clinic_error": "clinic_error",
        },
    )

    # ==============================================
    # سادساً: إنهاء الجولة بعد الرد
    # ==============================================

    builder.add_edge("safety", END)
    builder.add_edge("slot_followup", END)

    builder.add_edge("unsupported", END)

    builder.add_edge("clarify", END)

    builder.add_edge("clarify_provider", END)

    # بعد تجهيز الطلب نشغّل البحث الحقيقي بدل إنهاء الجولة.
    builder.add_edge("ready", "scheduler")

    # بعد البحث تنتهي الجولة وتعود المواعيد إلى المستخدم.
    builder.add_edge("scheduler", END)

    builder.add_edge("clinic_error", END)

    # تحويل تعريف العقد والمسارات إلى Graph جاهز.
    return builder.compile()



# نسخة جاهزة للاستخدام
graph = build_graph()
