
# scheduler.py
# هذا الملف مسؤول عن البحث عن المواعيد المتاحة في العيادة.
# يدعم اختيار الطبيب وفلترة المواعيد حسب الوقت.
# لا ينشئ حجوزات ولا يلغيها.

from agent.state import AgentState
from agent.time_normalization import normalize_time
from core.contracts import ClinicGateway


# البحث عن المواعيد الحقيقية باستخدام MCP Gateway.
async def find_available_slots(
    gateway: ClinicGateway,
    service_id: str,
    requested_date: str,
    provider_id: str | None = None,
    time_filters: dict | None = None,
) -> dict:

    # تجهيز بيانات البحث الأساسية.
    arguments = {
        "service_id": service_id,
        "date_from": requested_date,
        "date_to": requested_date,
        "limit": 5,
    }

    # إضافة الطبيب فقط إذا كان البحث مقيدًا بطبيب محدد.
    if provider_id is not None:
        arguments["provider_id"] = provider_id

    # إضافة حدود الوقت التي تم فهمها من طلب المريض.
    if time_filters:
        if time_filters.get("after") is not None:
            arguments["after"] = time_filters["after"]

        if time_filters.get("before") is not None:
            arguments["before"] = time_filters["before"]

    # إرسال طلب قراءة فقط إلى العيادة.
    return await gateway.call_read("find_slots", arguments)


# التحقق من طلب المريض والبحث عن المواعيد.
async def schedule(
    state: AgentState,
    gateway: ClinicGateway,
) -> dict:

    # قراءة الخدمة والتاريخ من الحالة المشتركة.
    service_id = state.get("service_id")
    requested_date = state.get("requested_date")

    # قراءة الوقت الأصلي وتحويله إلى حدود البحث.
    time_text = state.get("time_text")
    time_filters = normalize_time(time_text)

    # عدم تجاهل وقت غير مفهوم والبحث بدونه.
    if time_text and time_filters is None:
        return {
            "stage": "awaiting_time_clarification",
            "reply": "Please clarify your preferred appointment time.",
            "proposed_slots": [],
        }

    # قراءة معلومات اختيار الطبيب.
    provider_choice = state.get("provider_choice", "auto")
    provider_id = state.get("provider_id")
    preferred_provider_id = state.get("preferred_provider_id")

    # التأكد من تحديد الخدمة.
    if not service_id:
        return {
            "stage": "needs_service_mapping",
            "reply": "The appointment service must be identified first.",
            "proposed_slots": [],
        }

    # التأكد من تحديد تاريخ الموعد.
    if not requested_date:
        return {
            "stage": "awaiting_details",
            "reply": "Which date would you like to book?",
            "proposed_slots": [],
        }

    # عدم تجاهل اختيار طبيب محدد إذا لم يتم التعرف عليه.
    if provider_choice == "specific" and not provider_id:
        return {
            "stage": "awaiting_provider_clarification",
            "reply": "Please clarify which doctor you would like.",
            "proposed_slots": [],
        }

    # تحديد الطبيب المستخدم في البحث الأول.
    if provider_choice == "any":
        search_provider_id = None
    elif provider_choice == "specific":
        search_provider_id = provider_id
    else:
        search_provider_id = provider_id or preferred_provider_id

    # تسجيل ما إذا استخدمنا مواعيد بديلة.
    used_alternatives = False

    try:
        # البحث الأول مع تمرير الطبيب والوقت.
        result = await find_available_slots(
            gateway,
            service_id,
            requested_date,
            search_provider_id,
            time_filters,
        )

        # التأكد من نجاح الاتصال بالعيادة.
        if not isinstance(result, dict) or result.get("ok") is not True:
            return {
                "stage": "scheduler_error",
                "reply": "Unable to search for appointments right now.",
                "proposed_slots": [],
            }

        # استخراج المواعيد المتاحة.
        slots = result.get("slots", [])

        # التأكد من صحة نوع البيانات.
        if not isinstance(slots, list):
            return {
                "stage": "scheduler_error",
                "reply": "The clinic returned an invalid appointment response.",
                "proposed_slots": [],
            }

        # البحث عن أطباء بديلين فقط في الوضع التلقائي.
        if (
            not slots
            and provider_choice == "auto"
            and search_provider_id is not None
        ):
            result = await find_available_slots(
                gateway,
                service_id,
                requested_date,
                time_filters=time_filters,
            )

            # التأكد من نجاح البحث البديل.
            if not isinstance(result, dict) or result.get("ok") is not True:
                return {
                    "stage": "scheduler_error",
                    "reply": "Unable to search for alternative appointments.",
                    "proposed_slots": [],
                }

            # قراءة المواعيد البديلة.
            slots = result.get("slots", [])

            if not isinstance(slots, list):
                return {
                    "stage": "scheduler_error",
                    "reply": "The clinic returned an invalid appointment response.",
                    "proposed_slots": [],
                }

            used_alternatives = bool(slots)

    # التعامل مع أي خطأ دون اختراع مواعيد.
    except Exception:
        return {
            "stage": "scheduler_error",
            "reply": "Unable to connect to the clinic right now.",
            "proposed_slots": [],
        }

    # التعامل مع عدم وجود مواعيد مناسبة.
    if not slots:
        return {
            "stage": "no_slots",
            "reply": "No appointments are available for the requested date and time.",
            "proposed_slots": [],
        }

    # توضيح أن المواعيد من أطباء بديلين عند الحاجة.
    if used_alternatives:
        reply = (
            "No appointments were found with the preferred doctor. "
            "Alternative appointments are available. Please select a slot."
        )
    else:
        reply = (
            "Available appointments were found. "
            "Please select a slot."
        )

    # إعادة المواعيد الحقيقية دون تأكيد أو إنشاء أي حجز.
    return {
        "stage": "slots_proposed",
        "reply": reply,
        "proposed_slots": slots,
    }
