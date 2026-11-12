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
from agent.intake import IntakeResult
from agent.patient_identity import valid_patient_name, normalize_patient_phone, patient_name_noise, spoken_phone_digits
import re
from agent.clinic_inquiry import clinic_inquiry, inquiry_kind
from agent.dates import UNSPECIFIED_DATE, normalize_date, relative_date_text
from agent.times import (
    clocks_within_clinic_hours, has_incomplete_arabic_relative_time, is_appointment_time_change, normalize_time, period_reply,
    unclear_appointment_time_change,
)
from core.contracts import ClinicGateway
from agent.service_mapping import match_service
from agent.patient_memory import lookup_returning_patient
from agent.service_mapping import match_provider
from agent.scheduler_node import scheduler_node
from agent.slot_selection import (
    select_slot, extract_time, find_slots_by_id, extract_option_numbers,
    offered_clock_times, has_ambiguous_compact_time, format_slot_options, BARE_COMPACT_CLOCK,
    single_option_reference,
)
from agent.slot_handoff import (
    appointment_has_passed, build_find_slots_args, clinic_local_now, scheduling_signature, slot_matches_offer,
)
from agent.scheduler import extract_slots, filter_matching_slots, find_available_slots
from agent.relative_time import active_earlier_reference, search_earlier
from actions.approval import prepare_booking_action
from agent.presentation import display_time, say
from agent.service_mapping import service_question
from agent.slot_selection import has_slot_reference
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
    started = perf_counter()
    if state.get("selected_slot_id") and unclear_appointment_time_change(message):
        result = IntakeResult(language="ar")
    elif re.fullmatch(r"\s*(?:hello|hi|hey|مرحبا|مرحباً|اهلا|أهلا|السلام عليكم)[.!؟?،\s]*", message, re.I):
        result = IntakeResult(language=state.get("current_input_language") or ("ar" if re.search(r"[\u0600-\u06ff]", message) else "en"))
    elif (state.get("stage") in ("intake", "awaiting_details")
          and not state.get("service_id") and not state.get("proposed_slots")
          and (re.fullmatch(r"\s*بدي\s+[أا]حجز[.!؟?،\s]*", message)
               or (state.get("current_input_language") == "ar"
                   and re.fullmatch(r"\s*بدي\s+[أا]حرز[.!؟?،\s]*", message)))):
        # Treat the observed voice variant as booking intent only during service selection.
        result = IntakeResult(language="ar", intent="booking")
    elif state.get("proposed_slots") and (
        BARE_COMPACT_CLOCK.fullmatch(message)
        or (not state.get("selected_slot_id") and single_option_reference(state, message))
        or re.fullmatch(r"\s*(?:(?:I want |I choose )?(?:the )?(?:option(?: number)?\s+)?(?:one|two|three|first|second|third|\d{1,2})(?: option| one)?|(?:الخيار|الموعد)\s+(?:الأول|الثاني|الثالث|\d{1,2})|(?:الساعة\s+)?(?:three thirty|three and a half|ثلاثة ونص|3 ونص|\d{1,2}:\d{2}(?:\s*[AP]M)?))\s*[.!؟?]?", message, re.I)
    ):
        # الاختيار القصير المحدد لا يحتاج اتصال LLM؛ فحص guardrails المستقل يبقى أدناه.
        result = IntakeResult(language=state.get("current_input_language") or ("ar" if re.search(r"[\u0600-\u06ff]", message) else "en" if re.search(r"[a-z]", message, re.I) else state.get("language", "unknown")), intent="booking")
    else:
        result = await extract_request(message)
    if state.get("current_input_language") in ("ar", "en") and result.language != "unsupported":
        # لا تُمرر هذه القيمة إلا بعد فحص transcript من لغة التسجيل الصريحة.
        result = result.model_copy(update={"language": state["current_input_language"]})

    if result.intent in ("booking", "other") and active_earlier_reference(state):
        # A relative-time follow-up keeps the offer that gives the clock its meaning.
        result = result.model_copy(update={
            "time_text": None, "date_text": None, "service_text": None,
            "provider_text": None, "provider_any": False,
            "patient_name": None, "patient_phone": None,
        })

    # دمج المعلومات الجديدة مع السابقة
    updated_state = update_state_from_intake(state, result)
    updated_state["inquiry_reply"] = None
    updated_state["intake_ms"] = round((perf_counter() - started) * 1000, 2)
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
    time_text = state.get("time_text") or ""
    preference = normalize_time(time_text)
    message = state.get("current_message", "")
    literal_times = offered_clock_times(message)
    if literal_times is None and (preference["kind"] in ("ambiguous", "unrecognized")
                                  or relative_date_text(time_text)):
        literal_times = offered_clock_times(time_text)
    outside_hours = False
    has_range = bool(re.search(r"\b(?:after(?!\s+tomorrow\b)|before)\b|بعد\s+الساعة|قبل\s+الساعة", f"{time_text} {message}", re.I))
    if (has_ambiguous_compact_time(message) or has_incomplete_arabic_relative_time(message)
            or (literal_times == [] and not has_range)):
        preference.update(kind="unrecognized", requested_time=None, after=None, before=None)
    elif not has_range:
        # Use the same clock interpretations in both languages, including bare hours.
        if literal_times and len(literal_times) == 1:
            preference.update(kind="exact", requested_time=literal_times[0], after=None, before=None)
        elif literal_times and len(literal_times) == 2:
            preference.update(kind="ambiguous", requested_time=None, after=None, before=None)
        if preference["kind"] == "ambiguous":
            candidates = literal_times or offered_clock_times(time_text) or [
                clock for period in ("AM", "PM")
                if (clock := normalize_time(f"{time_text} {period}")["requested_time"])
            ]
            if len(candidates) == 2:
                within_hours = clocks_within_clinic_hours(state, candidates)
                outside_hours = within_hours == []
                if outside_hours:
                    hours = (state.get("clinic_calendar") or {}).get("opening_hours") or {}
                    # Infer a period only if just one candidate ever falls within regular opening hours.
                    within_hours = [clock for clock in candidates if any(
                        window and window["open"] <= clock < window["close"] for window in hours.values()
                    )]
                if within_hours and len(within_hours) == 1:
                    preference.update(kind="exact", requested_time=within_hours[0])
        if preference["kind"] == "exact" and preference["requested_time"]:
            outside_hours = clocks_within_clinic_hours(state, [preference["requested_time"]]) == []
    return {
        "time_kind": preference["kind"],
        "requested_time": preference["requested_time"],
        "time_after": preference["after"],
        "time_before": preference["before"],
        "time_outside_hours": outside_hours,
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
        return {"date_has_passed": False}

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
            "date_has_passed": False,
            "stage": "clinic_unavailable",
            "tool_calls": calls,
        }

    # تحويل التاريخ باستخدام الدالة الموجودة في dates.py
    clinic_now = clinic_local_now(clinic)
    clinic_today = clinic_now.date().isoformat() if clinic_now else clinic["today"]
    try:
        resolved_date = normalize_date(
            date_text,
            clinic_today,
            allow_past=True,
        )
    except ValueError:
        resolved_date = None
    if resolved_date is None:
        explicit = re.fullmatch(r"(?:يوم\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|الاثنين|الثلاثاء|الأربعاء|الخميس|الجمعة|السبت|الأحد)[,،]?\s+(.+)", date_text.strip(), re.I)
        if explicit and normalize_date(explicit.group(1), clinic_today, allow_past=True):
            # نحفظ العرض والاختيار السابقين أثناء السؤال، لكن لا تبقى موافقة قابلة للتنفيذ.
            prior = state.get("date_clarification_context") or {}
            return {**prior, "requested_date": prior.get("requested_date", state.get("requested_date_change")), "date_conflict": date_text,
                    "pending_action": None, "date_has_passed": False, "tool_calls": calls}

    # تخزين التاريخ النهائي داخل AgentState
    past = resolved_date is not None and resolved_date < clinic_today
    return {
        "requested_date": None if past else resolved_date,
        **({"date_text": resolved_date} if resolved_date and relative_date_text(date_text) else {}),
        "date_has_passed": past,
        **({"date_text": None, "pending_action": None, "proposed_slots": [], "selected_slot_id": None,
            "offer_context": None, "selection_candidates": []} if past else {}),
        "clinic_today": clinic_today,
        "date_clarification_context": None,
        "provider_catalog": clinic.get("providers", []),
        "clinic_calendar": {key: clinic.get(key) for key in ("opening_hours", "last_bookable_day", "policies", "now")},
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
        if not state.get("service_text"):
            return {"tool_calls": calls}
        return {
            "stage": "services_unavailable",
            "tool_calls": calls,
        }

    # مقارنة كلام المريض بالخدمات الرسمية.
    service_id = match_service(
        state.get("service_text") or "",
        result["services"]
    )

    return {
        "service_id": service_id,
        "patient_age_unverified": False if service_id == "pediatrics_visit" else state.get("patient_age_unverified", False),
        "service_catalog": result["services"],
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
) -> Literal["normal", "safety", "unsupported", "slot_followup", "date_conflict"]:

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

    if state.get("intent") == "cancel_booking" or state.get("cancellation_requested"):
        return "cancellation"
    if state.get("date_conflict"):
        # لا نعيد تطبيع تاريخ مؤكد أو نستدعي العيادة قبل حسم تعارض اليوم الجديد.
        return "date_conflict"
    if state.get("intent") in ("booking", "other") and active_earlier_reference(state):
        return "slot_followup"
    if (state.get("service_id") and state.get("requested_date")
            and (state.get("time_change_pending") or is_appointment_time_change(state.get("current_message", "")))):
        return "slot_followup"
    # نوجّه المتابعة بعد فحوص السلامة، بشرط بقاء المواعيد صالحة.
    if state.get("proposed_slots") and state.get("stage") in (
        "slots_proposed", "awaiting_slot_selection", "slot_selected",
        "awaiting_patient_details", "awaiting_phone_confirmation", "awaiting_approval",
    ) and not state.get("date_conflict"):
        return "slot_followup"

    # السماح بمتابعة المسار العادي.
    return "normal"


# تستقبل حالة المتابعة وتعيد الاختيار أو طلب البيانات أو الموافقة المعلقة.
# تتحقق من انتماء الاختيار للقائمة ومطابقته للطلب؛ لا تستخدم أدوات خارجية.
# الاسم والهاتف يكملان الاختيار السابق؛ ذكر رقم خيار أو وقت أو معرّف يعيد فحص الاختيار.
def slot_followup_node(state: AgentState) -> dict:
    updates = {}
    message = state.get("current_message", "")
    choosing = has_slot_reference(state, message)
    if patient_name_noise(state, message) or (state.get("selected_slot_id") and valid_patient_name(state.get("patient_name")) and
        state.get("confirmed_phone") != state.get("patient_phone") and spoken_phone_digits(message) is not None):
        choosing = False
    if not choosing and not state.get("selected_slot_id"):
        return {"reply": say(state, "Your appointment options are still available. Which option would you prefer?", "خيارات موعدك بعدها موجودة. أي خيار بناسبك؟")}
    if choosing or not state.get("selected_slot_id"):
        updates = select_slot(state, message)
        if updates.get("stage") != "slot_selected":
            old_slot = next((s for s in state.get("proposed_slots", []) if s.get("slot_id") == state.get("selected_slot_id")), None)
            if old_slot and slot_matches_offer(state, old_slot) and not has_incomplete_arabic_relative_time(message):
                updates["selected_slot_id"] = state["selected_slot_id"]
            return {**updates, "pending_action": None}

    # دمج محلي لأن LangGraph يطبّق تحديثات العقدة بعد إرجاعها فقط.
    selected_state = {**state, **updates}
    selected = next((slot for slot in selected_state.get("proposed_slots", [])
                     if slot.get("slot_id") == selected_state.get("selected_slot_id")), None)
    if selected is None or not slot_matches_offer(selected_state, selected):
        return {"stage": "awaiting_slot_selection", "selected_slot_id": None,
                "pending_action": None, "reply": say(state, "Please choose one of the available appointments.", "اختار موعد من الخيارات المتاحة، لو سمحت.")}

    if not valid_patient_name(selected_state.get("patient_name")) or normalize_patient_phone(selected_state.get("patient_phone")) is None:
        phone_question = say(state, "What is your phone number?", "شو رقم تلفونك؟")
        if selected_state.get("patient_phone_invalid") or selected_state.get("patient_phone"):
            phone_question = say(state, "Please provide a valid phone number starting with 0 or +970/+972.", "أعطيني رقم تلفون صحيح يبدأ بصفر أو +970/+972.")
            reason = selected_state.get("phone_error_reason")
            descriptions = {"length": ("Please repeat or type the full number. Local mobile numbers need 10 digits.", "احكي أو اكتب الرقم كامل. رقم الجوال المحلي لازم يكون 10 خانات."),
                            "prefix": ("The prefix is unsupported. Please repeat or type the full number.", "بداية الرقم غير مدعومة. احكي أو اكتب الرقم كامل."),
                            "format": ("Please say digits one by one or type the number.", "احكي الأرقام رقم رقم أو اكتب الرقم.")}
            if reason in descriptions:
                phone_question = say(state, *descriptions[reason])
            # نعرض التفريغ الرقمي المرفوض كما وصل، دون تخمين أرقام بديلة أو إرساله للسجل.
            candidate = selected_state.get("phone_candidate") or ""
            if re.fullmatch(r"[+\d\s()./-]+", candidate):
                phone_question = say(state, f"I recognized {candidate}. ", f"الرقم اللي وصلني {candidate}. ") + phone_question
        question = (say(state, "What is the patient's full name?", "شو اسم المريض الكامل؟") if not valid_patient_name(selected_state.get("patient_name"))
                    else phone_question)
        return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                "reply": question}

    # تأكيد الرقم مستقل عن موافقة الحجز، ويشير دائمًا إلى الرقم الحالي بعد آخر تصحيح.
    if selected_state.get("confirmed_phone") != selected_state["patient_phone"]:
        number = selected_state["patient_phone"]
        return {**updates, "stage": "awaiting_phone_confirmation", "pending_action": None,
                "reply": say(state, f"I heard your phone number as {number}. Is this correct? Say yes to confirm the number, or type the corrected number.",
                             f"سمعت رقم تلفونك {number}. الرقم صحيح؟ احكي نعم لتأكيد الرقم أو اكتب الرقم الصحيح.")}

    try:
        pending = prepare_booking_action(selected_state)
    except ValueError:
        # لا نتجاوز تحقق الموافقة، بما فيه شرط ولي الأمر لمسار الأطفال.
        if selected_state.get("patient_age_unverified"):
            return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                    "reply": say(state, "How old is the patient?", "كم عمر المريض؟")}
        if selected_state.get("requires_guardian") or selected_state.get("service_id") == "pediatrics_visit":
            return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                    "reply": say(state, "What is the guardian's full name?", "شو اسم ولي الأمر الكامل؟")}
        return {**updates, "stage": "awaiting_patient_details", "pending_action": None,
                "reply": say(state, "We need to verify the booking details first.", "لازم نتحقق من بيانات الحجز أولًا.")}
    existing = state.get("pending_action")
    reply = say(state, f"Please review and approve: {pending['summary']}", f"راجع التفاصيل ووافق لتأكيد الحجز: {pending['summary']}")
    if existing and existing.get("summary") == pending["summary"] and not choosing:
        reply = say(state, "Use Approve to confirm, or Reject to choose another appointment.",
                    "للتأكيد اضغط زر الموافقة، أو الرفض إذا بدك موعد تاني.")
    return {**updates, "stage": "awaiting_approval", "pending_action": pending, "reply": reply}


async def appointment_followup_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
    message = state.get("current_message", "")
    gateway = runtime.context["gateway"]
    if state.get("selected_slot_id") and unclear_appointment_time_change(message):
        selected = next((slot for slot in state.get("proposed_slots", [])
                         if slot["slot_id"] == state["selected_slot_id"]), None)
        if selected:
            clock = display_time(selected["start"], "ar")
            return {"pending_action": None,
                    "reply": f"موعدك المختار لسه الساعة {clock}. إذا بدك تغيّره، احكي الوقت الجديد بوضوح."}
    if state.get("intent") in ("booking", "other") and (reference := active_earlier_reference(state)):
        return await search_earlier(state, gateway, reference)
    if not (state.get("time_change_pending") or is_appointment_time_change(message)):
        calendar = state.get("clinic_calendar") or {}
        if not clinic_local_now(calendar):
            return slot_followup_node(state)
        try:
            clinic = await gateway.call_read("clinic_info", {})
        except Exception:
            clinic = {}
        if not isinstance(clinic, dict) or not clinic.get("ok") or not clinic_local_now(clinic):
            return {"pending_action": None, "reply": say(state,
                "I couldn't verify that the appointments are still available. Please try again.",
                "ما قدرت أتأكد إذا المواعيد لسه متاحة. جرّب مرة تانية، لو سمحت.")}
        clock_updates = {"clinic_today": clinic.get("today") or state.get("clinic_today"),
                         "clinic_calendar": {**calendar, "now": clinic["now"]}}
        refreshed = {**state, **clock_updates}
        if all(appointment_has_passed(refreshed, slot["date"], slot["start"])
               for slot in state.get("proposed_slots", [])):
            return {**clock_updates, "pending_action": None, "selected_slot_id": None, "proposed_slots": [],
                    "offer_context": None, "selection_candidates": [], "stage": "slot_unavailable",
                    "reply": say(state, "Those appointments have passed. Which upcoming time works for you?",
                                 "هالمواعيد مرقت. أي وقت جاي بناسبك؟")}
        return {**clock_updates, **slot_followup_node(refreshed)}

    # Retain the old choice while suspending its approval during clarification.
    waiting = {"pending_action": None, "time_change_pending": True, "stage": "awaiting_slot_selection"}
    cleared = {**waiting, "selected_slot_id": None}
    if is_appointment_time_change(message):
        # The clock parser expects the standalone Arabic clock cue.
        message = re.sub(r"\bل(?:ل)?ساع[ةه](?=\s)", "الساعة", message)
    times = offered_clock_times(message)
    period = period_reply(message)
    if times or is_appointment_time_change(message):
        waiting["time_change_clocks"] = []
    if period and state.get("time_change_pending") and not state.get("selection_candidates"):
        times = [clock for clock in state.get("time_change_clocks", []) if (clock < "12:00") == (period == "AM")]
        if len(times) == 1:
            clock = times[0]
            message = f"{int(clock[:2]) % 12 or 12}:{clock[3:]} {period}"
    current = {**state, **cleared, "current_message": message,
               "proposed_slots": [slot for slot in state.get("proposed_slots", [])
                                                      if slot_matches_offer(state, slot)]}
    # A bare number during time clarification is ambiguous with an option number.
    numbered = extract_option_numbers(message)
    explicit_option = re.search(r"\b(?:option|choice|choose|select|pick)\b|الخيار|خيار|أختار|اختار", message, re.I)
    followup_reference = has_slot_reference(current, message) and (numbered is None or explicit_option)
    if times or (times is None and state.get("time_change_pending") and followup_reference):
        choice = select_slot(current, message)
        if choice.get("selected_slot_id"):
            return {**cleared, **slot_followup_node(current), "time_change_pending": False}
        if choice.get("selection_candidates"):
            return {**choice, **waiting, "selected_slot_id": state.get("selected_slot_id")}
    if not times:
        if times == [] or is_appointment_time_change(message):
            waiting.update(time_change_clocks=[], selection_candidates=[])
        return {**waiting, "reply": say(state, "Please repeat the exact hour and minute.",
                                         "ممكن تعيد الساعة والدقيقة بالضبط؟")}

    # Search exact interpretations only, using the clinic's hours for this date.
    slots = {}
    try:
        clinic = await gateway.call_read("clinic_info", {})
        if not clinic.get("ok"):
            raise ValueError("Clinic information unavailable")
        within_hours = clocks_within_clinic_hours({**state, "clinic_calendar": clinic}, times)
        if within_hours is None and len(times) > 1:
            return {**waiting, "time_change_clocks": times, "selection_candidates": [],
                    "reply": say(state, "Did you mean morning or afternoon?",
                                             "قصدك صباحًا ولا مساءً؟")}
        if within_hours == []:
            return {**waiting, "reply": say(state, "That time is outside clinic opening hours.",
                                             "هالساعة خارج دوام العيادة.")}
        candidates = within_hours if within_hours is not None else times
        current_calendar = {**state, "clinic_today": clinic.get("today") or state.get("clinic_today"),
                            "clinic_calendar": clinic}
        future_times = [clock for clock in candidates
                        if not appointment_has_passed(current_calendar, state["requested_date"], clock)]
        if not future_times:
            return {**waiting, "reply": say(state, "That time has passed. Please choose a later appointment.",
                                             "هالساعة مرقت. اختار موعد بعد هالوقت، لو سمحت.")}
        for clock in future_times:
            search = {**current_calendar, "stage": "needs_slot_lookup", "time_kind": "exact", "requested_time": clock}
            result = await find_available_slots(gateway, build_find_slots_args(search, limit=50))
            for slot in filter_matching_slots(search, extract_slots(result)):
                slots[slot["slot_id"]] = slot
    except Exception:
        return {**waiting, "reply": say(state, "I couldn't verify the new time. Please try again.",
                                         "ما قدرت أتحقق من الساعة الجديدة. جرّب مرة تانية، لو سمحت.")}

    if not slots:
        return {**waiting, "reply": say(state, "No appointment is available at that time. Please choose another time.",
                                         "ما في موعد متاح بهالساعة. اختار ساعة تانية، لو سمحت.")}

    # Keep the search signature and expose only slots verified in the exact lookups.
    offers = {"proposed_slots": list(slots.values()), "offer_context": {
        "request": scheduling_signature(state),
        "search": {**scheduling_signature(state), "time_kind": "missing", "requested_time": None},
        "slot_ids": list(slots),
    }}
    verified = {**state, **cleared, **offers, "current_message": message}
    followup = slot_followup_node(verified)
    if followup.get("stage") == "awaiting_slot_selection":
        followup["reply"] = format_slot_options(offers["proposed_slots"], state) + "\n" + followup["reply"]
        followup["selected_slot_id"] = state.get("selected_slot_id")
    return {**waiting, **offers, **followup, "time_change_pending": not bool(followup.get("selected_slot_id")
                                                                              and followup.get("stage") != "awaiting_slot_selection")}

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
        "selection_candidates": [],
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

        # A phone lookup can suggest a provider but cannot establish the patient's name.

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

    if state.get("date_conflict"):
        return "clarify"
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

    if state.get("date_has_passed"):
        return "clarify"

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
    if state.get("date_has_passed"):
        return {"stage": "awaiting_details", "reply": say(state,
            "That date has passed. Which upcoming day works for you?",
            "هالتاريخ مرق. أي يوم جاي بناسبك؟")}
    if state.get("date_conflict"):
        if not state.get("requested_date"):
            return {"stage": "awaiting_details", "reply": say(state,
                f"The weekday conflicts with the calendar date in '{state['date_conflict']}'. Which full date do you intend?",
                f"اسم اليوم مختلف عن التاريخ في «{state['date_conflict']}». أي تاريخ كامل تقصد؟")}
        return {"stage": "awaiting_details", "reply": say(state,
            f"You previously requested {state.get('requested_date')}, which conflicts with {state['date_conflict']}. Please confirm the full date you want.",
            f"طلبك السابق ليوم {state.get('requested_date')} مختلف عن {state['date_conflict']}. ممكن تأكد التاريخ الكامل المطلوب؟")}

    if not state.get("service_id") and state.get("date_text") != UNSPECIFIED_DATE:
        reply = service_question(state)
    elif not state.get("requested_date"):
        reply = (
            "في أي يوم ترغب بحجز الموعد؟"
            if is_arabic
            else "Which day would you like the appointment?"
        )
    else:
        # إذا كان التاريخ معروفا لكن الخدمة غير محددة رسميا.
        reply = service_question(state)

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
    # الإلغاء المطلوب في السياسة عقدة محدودة، وليس نقل مواعيد أو إدارة عامة للحجوزات.
    async def cancellation_node(state, runtime: Runtime[GraphContext]):
        from actions.cancellation import propose_cancellation
        return await propose_cancellation(state, runtime.context["gateway"])
    builder.add_node("cancellation", cancellation_node)
    builder.add_edge("cancellation", END)

    # ==============================================
    # أولاً: تسجيل جميع العقد
    # ==============================================

    builder.add_node("intake", intake_node)
    builder.add_node("slot_followup", appointment_followup_node)

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
    builder.add_node("clinic_inquiry", clinic_inquiry)
    builder.add_conditional_edges(START, inquiry_kind, {"intake": "intake", "hours": "clinic_inquiry", "availability": "clinic_inquiry", "earliest": "clinic_inquiry"})
    builder.add_edge("clinic_inquiry", END)

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
            "date_conflict": "clarify",
            "cancellation": "cancellation",
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
