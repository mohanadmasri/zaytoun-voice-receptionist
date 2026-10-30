
# ==================================================
# dates.py
#
# تحويل التاريخ الذي يذكره المريض إلى YYYY-MM-DD.
# الاعتماد على تاريخ العيادة وليس تاريخ الجهاز.
# دعم التواريخ المباشرة وأيام الأسبوع وأسماء الأشهر.
# عدم قبول تاريخ قديم أو تاريخ غير صالح.
# المدخل نص التاريخ وتاريخ العيادة؛ المخرج ISO أو None عند الغموض أو التعارض.
# لا يستدعي LLM أو MCP؛ Graph يمرر تاريخ العيادة الموثق إلى هذه الدالة.
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


# أسماء الأشهر العربية الشائعة؛ المعرف الرقمي لا يعتمد على لغة STT أو لغة الجهاز.
ARABIC_MONTHS = {
    "يناير": 1, "كانون الثاني": 1,
    "فبراير": 2, "شباط": 2,
    "مارس": 3, "آذار": 3, "اذار": 3,
    "أبريل": 4, "ابريل": 4, "نيسان": 4,
    "مايو": 5, "أيار": 5, "ايار": 5,
    "يونيو": 6, "حزيران": 6,
    "يوليو": 7, "تموز": 7,
    "أغسطس": 8, "اغسطس": 8, "آب": 8, "اب": 8,
    "سبتمبر": 9, "أيلول": 9, "ايلول": 9,
    "أكتوبر": 10, "اكتوبر": 10, "تشرين الأول": 10, "تشرين الاول": 10,
    "نوفمبر": 11, "تشرين الثاني": 11,
    "ديسمبر": 12, "كانون الأول": 12, "كانون الاول": 12,
}

ARABIC_WEEKDAYS = {
    "الاثنين": 0, "الإثنين": 0, "الاثنان": 0,
    "الثلاثاء": 1, "الأربعاء": 2, "الاربعاء": 2,
    "الخميس": 3, "الجمعة": 4, "السبت": 5, "الأحد": 6, "الاحد": 6,
}


# تستقبل النص المنظف وتاريخ العيادة، وتعيد تاريخًا عربيًا محددًا أو None.
# السنة الناقصة تؤخذ من سنة العيادة؛ لا نقفز لسنة أخرى ولا نتجاهل تعارض يوم الأسبوع.
# مثال: يوم الاثنين 2 نوفمبر يختلف عن الاثنين وحدها؛ التاريخ المحدد له الأولوية.
def parse_arabic_calendar_date(text: str, today: date) -> str | None:
    month_names = "|".join(re.escape(name) for name in sorted(ARABIC_MONTHS, key=len, reverse=True))
    weekdays = "|".join(re.escape(name) for name in sorted(ARABIC_WEEKDAYS, key=len, reverse=True))
    match = re.fullmatch(
        rf"(?:يوم\s+)?(?:(?P<weekday>{weekdays})[,،]?\s+)?"
        rf"(?P<day>\d{{1,2}})\s+(?P<month>{month_names})(?:[,،]?\s+(?P<year>\d{{4}}))?",
        text,
    )
    if match is None:
        return None
    try:
        parsed_date = date(int(match.group("year") or today.year),
                           ARABIC_MONTHS[match.group("month")], int(match.group("day")))
    except ValueError:
        return None
    weekday = match.group("weekday")
    if parsed_date < today or (weekday and parsed_date.weekday() != ARABIC_WEEKDAYS[weekday]):
        return None
    return parsed_date.isoformat()


# تستقبل date_text الخام وclinic_today بصيغة ISO وتعيد تاريخًا صالحًا أو None.
# توحد الأرقام أولًا، ثم تفحص التاريخ العربي المحدد قبل تفسير يوم أسبوع منفرد.
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

    arabic_date = parse_arabic_calendar_date(text, today)
    if arabic_date is not None:
        return arabic_date

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
    arabic_days = ARABIC_WEEKDAYS

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
