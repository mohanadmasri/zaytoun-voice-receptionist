
# scheduler.py
# هذا الملف مسؤول عن البحث عن المواعيد الحقيقية في العيادة.
# يستخدم Slot Handoff لتجهيز شروط البحث والتحقق من النتائج.
# يدعم الطبيب المحدد والطبيب المفضل والبحث عن البدائل.
# لا ينشئ أي حجز ولا يلغي أي موعد.

from agent.state import AgentState
from agent.slot_handoff import (
    build_find_slots_args,
    slot_matches_request,
)
from core.contracts import ClinicGateway


# إرسال طلب البحث الحقيقي إلى العيادة عبر MCP.
async def find_available_slots(
    gateway: ClinicGateway,
    arguments: dict,
) -> dict:

    # استبعاد الحقول الفارغة من طلب البحث.
    clean_arguments = {
        key: value
        for key, value in arguments.items()
        if value is not None
    }

    # استخدام أداة قراءة فقط دون تنفيذ أي حجز.
    return await gateway.call_read(
        "find_slots",
        clean_arguments,
    )


# التأكد من صحة استجابة العيادة.
def extract_slots(result: dict) -> list[dict]:

    # يجب أن تكون الاستجابة ناجحة.
    if not isinstance(result, dict):
        raise ValueError("Invalid clinic response")

    if result.get("ok") is not True:
        raise ValueError("Clinic slot search failed")

    # يجب أن تحتوي الاستجابة على قائمة مواعيد.
    slots = result.get("slots")

    if not isinstance(slots, list):
        raise ValueError("Invalid slots list")

    return slots


# الاحتفاظ فقط بالمواعيد التي تطابق طلب المريض.
def filter_matching_slots(
    state: AgentState,
    slots: list[dict],
) -> list[dict]:

    # نستخدم دالة التحقق التي جهزها Developer A.
    return [
        slot
        for slot in slots
        if slot_matches_request(state, slot)
    ]


# البحث عن المواعيد بعد اكتمال معلومات Intake.
async def schedule(
    state: AgentState,
    gateway: ClinicGateway,
) -> dict:

    # قراءة طريقة اختيار الطبيب.
    provider_choice = state.get(
        "provider_choice",
        "auto",
    )

    # تحديد الطبيب الحالي والطبيب المفضل.
    provider_id = state.get("provider_id")
    preferred_provider_id = state.get(
        "preferred_provider_id"
    )

    # لا نبدأ البحث قبل أن تصبح معلومات Intake جاهزة.
    if state.get("stage") != "needs_slot_lookup":
        return {
            "stage": "awaiting_details",
            "reply": "The appointment request is not ready for scheduling.",
            "proposed_slots": [],
        }

    # لا نسمح بتجاهل الطبيب المحدد صراحة.
    if provider_choice == "specific" and not provider_id:
        return {
            "stage": "awaiting_provider_clarification",
            "reply": "Please clarify which doctor you would like.",
            "proposed_slots": [],
        }

    # تجهيز نسخة من الحالة للبحث الأول.
    search_state = dict(state)

    # اختيار الطبيب المناسب للبحث الأول.
    if provider_choice == "any":
        search_state["provider_id"] = None

    elif provider_choice == "specific":
        search_state["provider_id"] = provider_id

    else:
        search_state["provider_id"] = (
            provider_id or preferred_provider_id
        )

    # تجهيز معاملات MCP بالاعتماد على Slot Handoff.
    try:
        arguments = build_find_slots_args(
            search_state,
            limit=5,
        )

    # التعامل مع بيانات ناقصة أو غير صالحة.
    except (ValueError, TypeError) as error:

        # عدم تخمين الوقت إذا كان غامضا.
        if state.get("time_kind") in (
            "ambiguous",
            "unrecognized",
        ):
            return {
                "stage": "awaiting_time_clarification",
                "reply": "Please clarify your preferred appointment time.",
                "proposed_slots": [],
            }

        return {
            "stage": "awaiting_details",
            "reply": "Please clarify your appointment details.",
            "proposed_slots": [],
        }

    # حفظ حالة البحث عن أطباء بديلين.
    used_alternatives = False

    try:
        # البحث الأول باستخدام شروط المريض.
        result = await find_available_slots(
            gateway,
            arguments,
        )

        # استخراج المواعيد والتحقق من مطابقتها.
        slots = extract_slots(result)

        matching_slots = filter_matching_slots(
            search_state,
            slots,
        )

        # إذا لم تتوفر مواعيد للطبيب المفضل،
        # يمكن البحث عند أطباء آخرين في وضع auto فقط.
        if (
            not matching_slots
            and provider_choice == "auto"
            and search_state.get("provider_id") is not None
        ):

            # نسخ الحالة دون تقييد البحث بالطبيب المفضل.
            alternative_state = dict(search_state)
            alternative_state["provider_id"] = None

            # تجهيز شروط البحث البديل.
            alternative_arguments = build_find_slots_args(
                alternative_state,
                limit=5,
            )

            # البحث عن مواعيد بديلة بنفس التاريخ والوقت.
            alternative_result = await find_available_slots(
                gateway,
                alternative_arguments,
            )

            # قراءة النتائج والتحقق من مطابقتها.
            alternative_slots = extract_slots(
                alternative_result
            )

            matching_slots = filter_matching_slots(
                alternative_state,
                alternative_slots,
            )

            used_alternatives = bool(matching_slots)

    # التعامل مع استجابة غير صحيحة من العيادة.
    except ValueError:
        return {
            "stage": "scheduler_error",
            "reply": "Unable to verify available appointments right now.",
            "proposed_slots": [],
        }

    # التعامل مع أخطاء الاتصال أو الأدوات.
    except Exception:
        return {
            "stage": "scheduler_error",
            "reply": "Unable to connect to the clinic right now.",
            "proposed_slots": [],
        }

    # عدم اختراع مواعيد عند غياب النتائج.
    if not matching_slots:
        return {
            "stage": "no_slots",
            "reply": "No matching appointments are available for your request.",
            "proposed_slots": [],
        }

    # توضيح أن بعض المواعيد من البحث البديل.
    if used_alternatives:
        reply = (
            "No matching appointments were found with the preferred doctor. "
            "Alternative appointments are available. Please select a slot."
        )
    else:
        reply = (
            "Available appointments were found. "
            "Please select a slot."
        )

    # إعادة المواعيد الحقيقية دون إنشاء حجز.
    return {
        "stage": "slots_proposed",
        "reply": reply,
        "proposed_slots": matching_slots,
    }
