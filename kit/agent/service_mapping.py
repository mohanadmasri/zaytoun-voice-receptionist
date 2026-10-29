
# ==================================================
# تذكير — service_mapping.py
#
# الهدف: معرفة الخدمة الرسمية التي يقصدها المريض.
#
# 1. نستقبل اسم الخدمة الذي استخرجه Intake.
#
# 2. نستقبل قائمة الخدمات الرسمية من نظام العيادة.
#
# 3. نقارن النص بأسماء الخدمات بالعربية والإنجليزية.
#
# 4. إذا وجدنا تطابقا واحدا:
#    نعيد service_id الخاص به.
#
# 5. إذا لم نجد تطابقا أو كانت الخدمة غير واضحة:
#    نعيد None بدلا من التخمين.
#
# هذه الدالة لا تحجز مواعيد ولا تستدعي MCP.
# ==================================================

import re
import unicodedata
from typing import Any

# توحيد النص قبل المقارنة.
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

    # إزالة علامات الترقيم وتوحيد المسافات.
    return " ".join(re.findall(r"\w+", text))


# إيجاد معرف الخدمة الرسمية من كلام المريض.
def match_service(
    service_text: str,
    services: list[dict[str, Any]],
) -> str | None:

    requested = normalize_service_text(service_text)

    if not requested:
        return None

    matches = set()

    for service in services:

        service_id = service.get("id")

        if not isinstance(service_id, str):
            continue

        # الأسماء التي يسمح لنا نظام العيادة بمقارنتها.
        names = (
            service_id,
            service.get("name_ar"),
            service.get("name_en"),
        )

        for name in names:

            if not isinstance(name, str):
                continue

            official = normalize_service_text(name)

            if not official:
                continue

            # مقارنة كاملة أو تطابق عبارة ضمن عبارة أطول.
            same = requested == official

            partial = (
                f" {requested} " in f" {official} "
                or f" {official} " in f" {requested} "
            )

            if same or partial:
                matches.add(service_id)

    # نختار الخدمة فقط إذا كان هناك تطابق واحد.
    if len(matches) == 1:
        return next(iter(matches))

    return None


# ==================================================
# تذكير — match_provider
#
# الهدف: تحويل اسم الطبيب إلى provider_id رسمي.
#
# 1. نستقبل الاسم الذي استخرجه LLM.
# 2. نقارنه بأسماء الأطباء الحقيقية في العيادة.
# 3. ندعم الاسم الإنجليزي والعربي.
# 4. نتحقق أن الطبيب يقدم الخدمة المطلوبة.
# 5. لا نخمن إذا لم نجد تطابقا واضحا.
#
# الدالة لا تستدعي LLM ولا MCP.
# ==================================================

def match_provider(
    provider_text: str | None,
    providers: list[dict],
    service_id: str | None
) -> str | None:

    # لا يمكن المطابقة دون اسم طبيب وخدمة.
    if not provider_text or not service_id:
        return None

    # توحيد الاسم وإزالة بعض الألقاب الشائعة.
    def clean_name(name: str) -> str:
        normalized = normalize_service_text(name)

        titles = (
            "dr ",
            "doctor ",
            "د ",
            "دكتور ",
            "دكتورة ",
        )

        for title in titles:
            if normalized.startswith(title):
                return normalized[len(title):].strip()

        return normalized

    requested_name = clean_name(provider_text)

    matches = []

    # البحث في قائمة الأطباء الرسمية.
    for provider in providers:

        # تجاهل البيانات غير الصحيحة.
        if not isinstance(provider, dict):
            continue

        provider_id = provider.get("id")

        # يجب أن يكون الطبيب قادرا على تقديم الخدمة.
        if service_id not in provider.get("service_ids", []):
            continue

        # الأسماء الرسمية بالعربية والإنجليزية.
        english_name = clean_name(provider.get("name") or "")
        arabic_name = clean_name(provider.get("name_ar") or "")

        if requested_name in (english_name, arabic_name):
            if provider_id:
                matches.append(provider_id)

    # نقبل نتيجة واحدة واضحة فقط.
    if len(matches) == 1:
        return matches[0]

    return None
