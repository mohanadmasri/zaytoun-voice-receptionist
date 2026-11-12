
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

# أسماء أيام الشهر المنطوقة داخل تاريخ صريح فقط، ولا تحول second إلى تاريخ خارج هذا السياق.
ENGLISH_ORDINALS = dict(zip(
    ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
     "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth twenty-first "
     "twenty-second twenty-third twenty-fourth twenty-fifth twenty-sixth twenty-seventh twenty-eighth "
     "twenty-ninth thirtieth thirty-first").split(), range(1, 32)))


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
    "اثنين": 0, "إثنين": 0, "ثلاثاء": 1, "أربعاء": 2, "اربعاء": 2,
    "خميس": 3, "جمعة": 4, "سبت": 5, "أحد": 6, "احد": 6,
    "الاثنين": 0, "الإثنين": 0, "الاثنان": 0,
    "الثلاثاء": 1, "الأربعاء": 2, "الاربعاء": 2,
    "الخميس": 3, "الجمعة": 4, "السبت": 5, "الأحد": 6, "الاحد": 6,
}


ARABIC_DATE_DAYS = {"الأول": 1, "الاول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4,
                   "الخامس": 5, "السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10}
ARABIC_DATE_DAYS.update(dict(zip(("الحادي عشر", "الثاني عشر", "الثالث عشر", "الرابع عشر", "الخامس عشر",
    "السادس عشر", "السابع عشر", "الثامن عشر", "التاسع عشر", "العشرين", "الحادي والعشرين",
    "الثاني والعشرين", "الثالث والعشرين", "الرابع والعشرين", "الخامس والعشرين", "السادس والعشرين",
    "السابع والعشرين", "الثامن والعشرين", "التاسع والعشرين", "الثلاثين", "الحادي والثلاثين"), range(11, 32))))
ARABIC_DATE_DAYS.update(dict(zip("واحد اثنين ثلاثة أربعة خمسة ستة سبعة ثمانية تسعة عشرة".split(), range(1, 11))))
ARABIC_DATE_DAYS.update(dict.fromkeys(
    ("الثانية عشر", "الثانية عشرة", "الثاني عشرة", "اثنا عشر", "اثني عشر", "إثنا عشر", "إثني عشر"), 12,
))
ARABIC_RELATIVE_DAYS = {
    "اليوم": 0, "بكرا": 1, "بكرة": 1, "غدا": 1,
    "بعد بكرا": 2, "بعد بكرة": 2, "بعد غد": 2, "بعد يومين": 2,
    "بعد ثلاثة أيام": 3, "بعد ثلاثة ايام": 3,
}
ENGLISH_RELATIVE_DAYS = {"today": 0, "tomorrow": 1, "day after tomorrow": 2, "the day after tomorrow": 2}
UNSPECIFIED_DATE = "unspecified date"
ENGLISH_DATE_DAYS = {**ENGLISH_ORDINALS, **dict(zip(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(1, 20),
)), "twenty": 20, "thirty": 30, "thirty-one": 31}
ENGLISH_DATE_DAYS.update({f"twenty-{word}": 20 + number for word, number in ENGLISH_DATE_DAYS.items() if number < 10 and word.isalpha() and word not in ENGLISH_ORDINALS})


def _date_words(words) -> str:
    return "|".join(re.escape(word).replace(r"\-", "[- ]") for word in sorted(words, key=len, reverse=True))


_ENGLISH_DATE_WORD = _date_words(ENGLISH_DATE_DAYS)
_ENGLISH_DATE_DAY = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{_ENGLISH_DATE_WORD})"
_ENGLISH_MONTH = rf"(?:{_date_words(ENGLISH_MONTHS)})"
_ARABIC_DATE_DAY = rf"(?:\d{{1,2}}|{_date_words(ARABIC_DATE_DAYS)})"
_EXPLICIT_CALENDAR_DATE = re.compile(
    rf"(?<!\w)(?:(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)[, ]+)?"
    rf"(?:{_ENGLISH_MONTH}\s+{_ENGLISH_DATE_DAY}|{_ENGLISH_DATE_DAY}\s+(?:of\s+)?{_ENGLISH_MONTH})"
    rf"|(?:يوم\s+)?(?:(?:{_date_words(ARABIC_WEEKDAYS)})[,، ]+)?"
    rf"{_ARABIC_DATE_DAY}\s+(?:من\s+)?(?:{_date_words(ARABIC_MONTHS)}))"
    r"(?:[,،]?\s+\d{4})?(?!\w)", re.I,
)


_RELATIVE_DATE = re.compile(
    rf"(?<!\w)(?:{_date_words(ARABIC_RELATIVE_DAYS | ENGLISH_RELATIVE_DAYS)}"
    rf"|(?:{_date_words(ARABIC_WEEKDAYS)}) (?:الجاي|الجاية|القادم|القادمة)"
    r"|next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?:الأسبوع|الاسبوع) (?:الجاي|القادم)|next week)(?!\w)", re.I,
)


def relative_date_text(message: str) -> str | None:
    text = " ".join(re.sub(r"[ً-ٰٟ]", "", message).casefold().split())
    matches = set(_RELATIVE_DATE.findall(text))
    if len(matches) > 1:
        return UNSPECIFIED_DATE
    phrase = next(iter(matches), None)
    if phrase in ("الأسبوع الجاي", "الاسبوع الجاي", "الأسبوع القادم", "الاسبوع القادم", "next week"):
        return UNSPECIFIED_DATE
    return phrase


def explicit_calendar_date_text(message: str) -> str | None:
    """Recover a literal date span without choosing between conflicting dates."""
    text = " ".join(re.sub(r"[\u064b-\u065f\u0670]", "", message).split())
    matches = list(_EXPLICIT_CALENDAR_DATE.finditer(text))
    if not matches:
        return None
    if len({match[0].casefold() for match in matches}) > 1:
        return text
    match = matches[0]
    if re.match(rf"\s+(?:or|أو|او)\s+(?:{_ENGLISH_DATE_DAY}|{_ARABIC_DATE_DAY})(?!\w)", text[match.end():], re.I):
        return text
    return match[0]


def arabic_day_reply(message: str) -> str | None:
    """Recover a standalone Arabic weekday or relative day from a date reply."""
    text = " ".join(re.sub(r"[\u064b-\u065f\u0670]", "", message).strip().strip(".,!?؟،").split())
    text = re.sub(r"^في\s+", "", text)
    if text in ARABIC_RELATIVE_DAYS or text.removeprefix("يوم ") in ARABIC_WEEKDAYS:
        return text
    return None


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
    if weekday and parsed_date.weekday() != ARABIC_WEEKDAYS[weekday]:
        return None
    return parsed_date.isoformat()


# تستقبل date_text الخام وclinic_today بصيغة ISO وتعيد تاريخًا صالحًا أو None.
# توحد الأرقام أولًا، ثم تفحص التاريخ العربي المحدد قبل تفسير يوم أسبوع منفرد.
def normalize_date(
    date_text: str | None,
    clinic_today: str,
    *,
    allow_past: bool = False,
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
    text = re.sub(r"^(?:on|في)\s+", "", text)
    text = re.sub(r"\s+من\s+(?=(?:" + "|".join(ARABIC_MONTHS) + r")\b)", " ", text)
    for word in sorted(ARABIC_DATE_DAYS, key=len, reverse=True):
        number = ARABIC_DATE_DAYS[word]
        text = re.sub(r"(?<!\w)" + word + r"(?=\s+(?:" + "|".join(ARABIC_MONTHS) + r")\b)", str(number), text)
    # السنة الناقصة تؤخذ من تقويم العيادة، ولا نتجاوز إلى سنة أخرى تلقائيًا.
    months = "|".join(ENGLISH_MONTHS)
    text = re.sub(r"\s+of\s+(?=(?:" + months + r")\b)", " ", text)
    day = _ENGLISH_DATE_DAY
    if re.fullmatch(rf"(?:(?:{months})\s+{day}|{day}\s+(?:{months}))", text):
        text += f", {today.year}"

    # تحويل الأرقام العربية والفارسية.
    text = text.translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    ))
    english_days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    explicit_weekday = re.fullmatch(r"(" + "|".join(english_days) + r")[,]?\s+(.+)", text)
    if explicit_weekday:
        resolved = normalize_date(explicit_weekday.group(2), clinic_today, allow_past=allow_past)
        return resolved if resolved and date.fromisoformat(resolved).weekday() == english_days[explicit_weekday.group(1)] else None
    ordinal_names = _ENGLISH_DATE_WORD
    # November second, 2026 وsecond November 2026 يصبحان صيغًا رقمية مدعومة أصلًا.
    for pattern in (rf"(?P<month>{'|'.join(ENGLISH_MONTHS)})\s+(?P<day>{ordinal_names}),?\s+(?P<year>\d{{4}})",
                    rf"(?P<day>{ordinal_names})\s+(?P<month>{'|'.join(ENGLISH_MONTHS)}),?\s+(?P<year>\d{{4}})"):
        ordinal = re.fullmatch(pattern, text)
        if ordinal:
            day_number = ENGLISH_DATE_DAYS[ordinal.group("day").replace(" ", "-")]
            text = f"{ordinal.group('month')} {day_number}, {ordinal.group('year')}"
            break

    arabic_date = parse_arabic_calendar_date(text, today)
    if arabic_date is not None:
        return arabic_date if allow_past or arabic_date >= clinic_today else None

    relative_days = ARABIC_RELATIVE_DAYS.get(text, ENGLISH_RELATIVE_DAYS.get(text))
    if relative_days is not None:
        return (today + timedelta(days=relative_days)).isoformat()

    # قبول التاريخ بصيغة YYYY-MM-DD.
    try:
        exact_date = date.fromisoformat(text)

        return (
            exact_date.isoformat()
            if allow_past or exact_date >= today
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
        if not allow_past and parsed_date < today:
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
