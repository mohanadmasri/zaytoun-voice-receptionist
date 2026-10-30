
# ==================================================
# service_mapping.py
#
# تحويل اسم الخدمة الذي ذكره المريض إلى معرف رسمي.
# المقارنة تعتمد على الخدمات الحقيقية من نظام العيادة.
# دعم العربية والإنجليزية واختلاف علامات الترقيم.
# عدم اختيار خدمة عند وجود أكثر من تطابق.
# المدخل نص الخدمة وقائمتها الرسمية؛ المخرج معرف موثق أو None. لا توجد اتصالات خارجية.
# ==================================================

import re
import unicodedata
from typing import Any


# توحيد النص لتسهيل مقارنة الأسماء.
def normalize_service_text(text: str) -> str:

    text = unicodedata.normalize("NFKC", text).casefold()

    # إزالة التشكيل العربي.
    text = "".join(
        char
        for char in text
        if unicodedata.category(char) != "Mn"
    )

    # توحيد أشكال حرف الألف.
    for letter in ("أ", "إ", "آ", "ٱ"):
        text = text.replace(letter, "ا")

    # توحيد اختلاف check-up وcheckup.
    text = re.sub(r"\bcheck[\s\-_]+up\b", "checkup", text)

    # إزالة علامات الترقيم وتوحيد المسافات.
    return " ".join(re.findall(r"\w+", text))


# توحد أسنان/الأسنان بعد إزالة الهمزات، داخل مقارنة الخدمات فقط.
# لا تغير normalize_service_text المشتركة مع مطابقة أسماء الأطباء.
def normalize_service_name(text: str) -> str:
    normalized = normalize_service_text(text)
    return re.sub(r"\b(?:الاسنان|اسناني|سناني)\b", "اسنان", normalized)


# تلتقط دليل الإجراء الصريح من رسالة المريض، لا من تلخيص LLM الذي قد يعيده لاختصاص عام.
# تعيد نص خدمة للمطابقة اللاحقة مع الكتالوج، لا معرفًا مخترعًا؛ تعدد الإجراءات يبقى غامضًا.
def explicit_service_evidence(message: str) -> str | None:
    text = normalize_service_name(message)
    dental = bool(re.search(r"اسنان|\bdental\b|\bdentist\b|\bteeth\b", text))
    procedures = {
        "checkup": r"\bcheckup\b|\b(?:فحص|اكشف|كشف)\b",
        "cleaning": r"\bcleaning\b|\bscaling\b|تنظيف|تنضيف",
        "filling": r"\bfilling\b|حشو|حشوة|حشوات",
    }
    matches = [name for name, pattern in procedures.items() if re.search(pattern, text)]
    if not matches:
        return None
    # العبارات الإنجليزية الصريحة أو جواب قصير باسم الخدمة لا يحتاجان تخمين تخصص جديد.
    short = bool(re.fullmatch(r"(?:please\s+)?(?:checkup|cleaning|filling)(?:\s+please)?", text))
    if not dental and not short:
        return None
    if len(matches) > 1:
        return message
    if dental:
        return {"checkup": "dental checkup", "cleaning": "dental cleaning", "filling": "dental filling"}[matches[0]]
    return matches[0]


# تفهم نوع إجراء الأسنان المصرح به، ولا تحول طلب طبيب أسنان عام إلى فحص تلقائي.
# لا تعيد إلا معرفًا موجودًا في كتالوج MCP؛ تعدد الإجراءات يظل غامضًا.
def dental_aliases(text: str, services: list[dict]) -> set[str]:
    text = normalize_service_name(text)
    if not re.search(r"اسنان|\bdental\b|\bdentist\b|\bteeth\b", text):
        return set()
    kinds = {
        "dental_checkup": r"\b(?:فحص|اكشف|كشف)\b|\bcheckup\b|\bexamination\b",
        "dental_cleaning": r"تنظيف|تنضيف|\bcleaning\b|\bscaling\b",
        "dental_filling": r"حشو|حشوة|حشوات|\bfilling\b",
    }
    available = {service.get("id") for service in services}
    return {service_id for service_id, pattern in kinds.items()
            if service_id in available and re.search(pattern, text)}


# تصوغ سؤالًا واحدًا عن خدمة أسنان عامة، وتذكر الخيارات الموجودة فعليًا فقط.
def service_question(state: dict) -> str:
    from agent.presentation import say
    text = normalize_service_name(state.get("service_text") or "")
    options = [s for s in state.get("service_catalog", [])
               if str(s.get("id", "")).startswith("dental_")]
    if re.search(r"اسنان|\bdentist\b|\bdental\b", text) and options:
        names = [s.get("name_ar") if state.get("language") == "ar" else s.get("name_en") for s in options]
        names = [name for name in names if name]
        if names:
            return say(state, "Would you like " + ", ".join(names) + "?",
                       "بدك " + "، أو ".join(names) + "؟")
    return say(state, "What type of appointment would you like?", "شو نوع الموعد اللي بدك إياه؟")


# تستقبل كلام المريض وقائمة الخدمات الرسمية، وتعيد معرف خدمة وحيدة دون تخمين.
# يبقى تعدد التطابقات سببًا لإرجاع None وطلب توضيح.
def match_service(
    service_text: str,
    services: list[dict[str, Any]],
) -> str | None:

    requested = normalize_service_name(service_text)

    if not requested:
        return None

    aliases = dental_aliases(service_text, services)
    if len(aliases) > 1:
        return None

    # المرادف يضيف مرشحًا ولا يتجاوز وجود خدمة رسمية أخرى مطابقة بنفس الاسم.
    matches = set(aliases)

    for service in services:

        # تجاهل البيانات غير الصحيحة.
        if not isinstance(service, dict):
            continue

        service_id = service.get("id")

        if not isinstance(service_id, str):
            continue

        # مقارنة النص مع المعرف والأسماء الرسمية.
        names = (
            service_id,
            service.get("name_ar"),
            service.get("name_en"),
        )

        for name in names:

            if not isinstance(name, str):
                continue

            official = normalize_service_name(name)

            if not official:
                continue

            # تطابق كامل أو تطابق عبارة كاملة.
            same = requested == official

            partial = (
                f" {requested} " in f" {official} "
                or f" {official} " in f" {requested} "
            )

            if same or partial:
                matches.add(service_id)

    # اعتماد نتيجة واحدة فقط دون تخمين.
    if len(matches) == 1:
        return next(iter(matches))

    return None


# ==================================================
# التحقق من هوية الطبيب.
#
# مطابقة اسم الطبيب مع أسماء الأطباء الرسمية.
# التأكد أن الطبيب يقدم الخدمة المطلوبة.
# عدم اختيار طبيب عند وجود أكثر من تطابق.
# ==================================================

def match_provider(
    provider_text: str | None,
    providers: list[dict],
    service_id: str | None,
) -> str | None:

    # لا يمكن تحديد الطبيب دون خدمة واسمه.
    if not provider_text or not service_id:
        return None

    # إزالة الألقاب الشائعة قبل مطابقة الاسم.
    def clean_name(name: str) -> str:
        normalized = normalize_service_text(name)

        titles = (
            "dr ",
            "doctor ",
            "الدكتور ",
            "الدكتورة ",
            "دكتور ",
            "دكتورة ",
            "د ",
        )

        for title in titles:
            if normalized.startswith(title):
                return normalized[len(title):].strip()

        return normalized

    requested_name = clean_name(provider_text)

    matches = []

    # البحث داخل قائمة الأطباء الرسمية.
    for provider in providers:

        if not isinstance(provider, dict):
            continue

        provider_id = provider.get("id")

        # الطبيب يجب أن يقدم الخدمة المطلوبة.
        if service_id not in provider.get("service_ids", []):
            continue

        english_name = clean_name(
            provider.get("name") or ""
        )

        arabic_name = clean_name(
            provider.get("name_ar") or ""
        )

        if requested_name in (
            english_name,
            arabic_name,
        ):
            if provider_id:
                matches.append(provider_id)

    # نختار الطبيب فقط عند وجود تطابق واحد.
    if len(matches) == 1:
        return matches[0]

    return None
