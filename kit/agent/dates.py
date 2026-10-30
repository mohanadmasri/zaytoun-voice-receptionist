
# ==================================================
# dates.py
#
# تحويل التاريخ الذي يذكره المريض إلى YYYY-MM-DD.
# الاعتماد على تاريخ العيادة وليس تاريخ الجهاز.
# دعم التواريخ المباشرة وأيام الأسبوع وأسماء الأشهر.
# عدم قبول تاريخ قديم أو تاريخ غير صالح.
# ==================================================

import re
from datetime import date, timedelta


# أسماء الأشهر الإنجليزية واختصاراتها.
ENGLISH_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


# تحويل التاريخ اعتمادا على تاريخ العيادة.
def normalize_date(
    date_text: str | None,
    clinic_today: str,
) -> str | None:

    # عدم تفسير قيمة فارغة.
    if not date_text or not date_text.strip():
        return None

    # قراءة تاريخ العيادة.
    today = date.fromisoformat(clinic_today)

    # تنظيف النص وإزالة التشكيل العربي.
    text = date_text.strip().lower()
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = " ".join(text.strip(".,!?؟،").split())

    # تحويل الأرقام العربية والفارسية.
    text = text.translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    ))

    # اليوم والغد وبعد الغد.
    if text in ("today", "اليوم"):
        return today.isoformat()

    if text in ("tomorrow", "بكرا", "بكرة", "غدا"):
        return (today + timedelta(days=1)).isoformat()

    if text in (
        "day after tomorrow",
        "بعد بكرا",
        "بعد بكرة",
        "بعد غد",
    ):
        return (today + timedelta(days=2)).isoformat()

    # قبول التاريخ بصيغة YYYY-MM-DD.
    try:
        exact_date = date.fromisoformat(text)

        return (
            exact_date.isoformat()
            if exact_date >= today
            else None
        )

    except ValueError:
        pass

    # دعم صيغ مثل:
    # November 2, 2026
    # Nov 2 2026
    # 2 November 2026
    # 2nd November 2026

    month_names = "|".join(
        sorted(
            ENGLISH_MONTHS,
            key=len,
            reverse=True,
        )
    )

    # الشهر ثم اليوم ثم السنة.
    month_first = re.fullmatch(
        rf"({month_names})\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?"
        r",?\s+(\d{4})",
        text,
    )

    # اليوم ثم الشهر ثم السنة.
    day_first = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+"
        rf"({month_names})"
        r",?\s+(\d{4})",
        text,
    )

    if month_first or day_first:

        if month_first:
            month = ENGLISH_MONTHS[month_first.group(1)]
            day = int(month_first.group(2))
            year = int(month_first.group(3))

        else:
            day = int(day_first.group(1))
            month = ENGLISH_MONTHS[day_first.group(2)]
            year = int(day_first.group(3))

        # رفض تواريخ غير موجودة مثل 31 February.
        try:
            parsed_date = date(year, month, day)
        except ValueError:
            return None

        # عدم قبول مواعيد قبل تاريخ العيادة.
        if parsed_date < today:
            return None

        return parsed_date.isoformat()

    # أسماء أيام الأسبوع بالإنجليزية.
    english_days = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    # أسماء أيام الأسبوع بالعربية.
    arabic_days = {
        "الاثنين": 0,
        "الإثنين": 0,
        "الثلاثاء": 1,
        "الأربعاء": 2,
        "الاربعاء": 2,
        "الخميس": 3,
        "الجمعة": 4,
        "السبت": 5,
        "الأحد": 6,
        "الاحد": 6,
    }

    # إزالة الكلمات الإضافية حول أيام الأسبوع.
    day_text = re.sub(
        r"^(next|this)\s+",
        "",
        text,
    )

    day_text = re.sub(
        r"^يوم\s+",
        "",
        day_text,
    )

    day_text = re.sub(
        r"\s+(القادم|القادمة|الجاي|الجاية)$",
        "",
        day_text,
    )

    # تحديد يوم الأسبوع.
    target_day = english_days.get(day_text)

    if target_day is None:
        target_day = arabic_days.get(day_text)

    # إذا لم نفهم اليوم، نطلب توضيحا.
    if target_day is None:
        return None

    # حساب عدد الأيام حتى اليوم المطلوب.
    days_ahead = (
        target_day - today.weekday()
    ) % 7

    # إذا كان اليوم نفسه نختار الأسبوع القادم.
    if days_ahead == 0:
        days_ahead = 7

    return (
        today + timedelta(days=days_ahead)
    ).isoformat()
