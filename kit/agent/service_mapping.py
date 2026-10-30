
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
    return re.sub(r"\bالاسنان\b", "اسنان", normalize_service_text(text))


# تستقبل كلام المريض وقائمة الخدمات الرسمية، وتعيد معرف خدمة وحيدة دون تخمين.
# يبقى تعدد التطابقات سببًا لإرجاع None وطلب توضيح.
def match_service(
    service_text: str,
    services: list[dict[str, Any]],
) -> str | None:

    requested = normalize_service_name(service_text)

    if not requested:
        return None

    matches = set()

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
