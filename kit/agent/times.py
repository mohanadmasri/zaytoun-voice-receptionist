
# times.py
# هذا الملف يحلل الوقت الذي يذكره المريض.
# يميز بين الساعة المحددة والفترة الزمنية.
# يدعم العربية والإنجليزية ولا ينشئ أي حجز.

import re
from datetime import date
from typing import Literal, TypedDict


# تحديد شكل نتيجة تحليل الوقت.
class TimePreference(TypedDict):
    kind: Literal[
        "exact",
        "window",
        "missing",
        "ambiguous",
        "unrecognized",
    ]
    requested_time: str | None
    after: str | None
    before: str | None


# إنشاء نتيجة موحدة لجميع الحالات.
def _result(
    kind: str,
    requested_time: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> TimePreference:
    return {
        "kind": kind,
        "requested_time": requested_time,
        "after": after,
        "before": before,
    }


# أسماء الساعات المكتوبة بالكلمات العربية.
ARABIC_HOURS = {
    "واحد": 1, "واحدة": 1, "اثنين": 2, "اثنتين": 2, "إثنتين": 2, "اتنين": 2, "تنتين": 2,
    "ثلاثة": 3, "تلاتة": 3, "ثلاث": 3, "تلات": 3, "أربعة": 4, "اربعة": 4, "أربع": 4, "اربع": 4, "خمسة": 5,
    "ستة": 6, "سبعة": 7, "ثمانية": 8, "تمانية": 8, "تسع": 9, "تسعة": 9, "عشرة": 10,
    "الواحدة": 1,
    "الاولى": 1,
    "الثانية": 2,
    "الثالثة": 3,
    "الرابعة": 4,
    "الخامسة": 5,
    "السادسة": 6,
    "السابعة": 7,
    "الثامنة": 8,
    "التاسعة": 9,
    "العاشرة": 10,
    "الحادية عشرة": 11,
    "الثانية عشرة": 12,
    "حدعش": 11,
    "تناش": 12,
    "اتناش": 12,
}

_ARABIC_MINUTE_UNITS = {
    "واحد": 1, "واحدة": 1, "اثنين": 2, "اتنين": 2, "ثلاث": 3, "ثلاثة": 3,
    "تلات": 3, "تلاتة": 3, "أربع": 4, "اربعة": 4, "أربعة": 4, "خمس": 5, "خمسة": 5,
    "ست": 6, "ستة": 6, "سبع": 7, "سبعة": 7, "ثمان": 8, "ثمانية": 8, "تسع": 9, "تسعة": 9,
}
_ARABIC_MINUTE_TENS = {
    "عشرين": 20, "ثلاثين": 30, "تلاتين": 30, "أربعين": 40, "اربعين": 40, "خمسين": 50,
}
_ARABIC_MINUTE_WORDS = {**_ARABIC_MINUTE_UNITS, **_ARABIC_MINUTE_TENS, "عشر": 10, "عشرة": 10}
_ARABIC_MINUTES = "|".join(sorted(_ARABIC_MINUTE_WORDS, key=len, reverse=True))
_ARABIC_UNITS = "|".join(sorted(_ARABIC_MINUTE_UNITS, key=len, reverse=True))
_ARABIC_TENS = "|".join(_ARABIC_MINUTE_TENS)
_ARABIC_HOUR_WORDS = "|".join(re.escape(word) for word in sorted(ARABIC_HOURS, key=len, reverse=True))
_UNPARSED_ARABIC_MINUTES = re.compile(
    rf"(?:^|\bالساع[ةه]\s+)(?:{_ARABIC_HOUR_WORDS}|\d{{1,2}})\s*و"
)
_MALFORMED_NUMERIC_CLOCK = re.compile(r"\d\s*:\s*(?:\d{3,}|\d+\s*:\s*\d)")
_ARABIC_RELATIVE_TIME = re.compile(
    rf"(?<!\w)(?P<hour>\d{{1,2}}|(?:ال)?(?:{_ARABIC_HOUR_WORDS}))\s+"
    r"(?:[إا]لّ?ا|[إا]لى)(?!\w)(?P<quarter>\s+(?:ال)?ربع(?!\w))?"
)


def is_appointment_time_change(message: str) -> bool:
    return bool(re.search(
        r"\b(?:change|move)\s+(?:(?:my|the)\s+)?(?:time\b|appointment\s+(?:time\b|"
        r"to\s+(?=\d{3}\b|\d{1,2}(?::|\s*[ap]\.?\s*m\b|\s*[.!?]?\s*$))))"
        r"|(?:[أا]?غيّ?ر|نغيّ?ر)\s+(?:الوقت|الساعة|(?:ال)?موعد(?:ي)?\s+(?:ل(?:ل)?ساعة|إلى الساعة|الى الساعة))",
        message, re.I,
    ))


def unclear_appointment_time_change(message: str) -> bool:
    """Recognize the observed ambiguous STT wording without treating it as consent."""
    text = re.sub(r"[\u064b-\u065f]", "", message)
    return bool(re.fullmatch(r"\s*بدأ\s+غير\s+(?:ال)?موعد\s+الساع[ةه]\s+\d{1,2}(?::\d{2})?[.!؟?،\s]*", text))


def clocks_within_clinic_hours(state: dict, clocks: list[str]) -> list[str] | None:
    """Return in-hours candidates, or None when the date's hours are unknown."""
    day = state.get("requested_date")
    if not day:
        return None
    weekday = date.fromisoformat(day).strftime("%A").lower()
    calendar = ((state.get("clinic_calendar") or {}).get("opening_hours") or {})
    if weekday not in calendar:
        return None
    hours = calendar[weekday]
    return [clock for clock in clocks if hours and hours["open"] <= clock < hours["close"]]


def _arabic_relative_hour(match: re.Match) -> int:
    hour = match["hour"]
    if hour.isdecimal():
        return int(hour)
    return ARABIC_HOURS.get(hour, ARABIC_HOURS.get(hour.removeprefix("ال"), 0))


def has_incomplete_arabic_relative_time(message: str) -> bool:
    # An unfinished or unsupported relative time cannot become a whole hour.
    return any(not match["quarter"] or not 1 <= _arabic_relative_hour(match) <= 12
               for match in _ARABIC_RELATIVE_TIME.finditer(message))


def has_malformed_clock(normalized_text: str) -> bool:
    return bool(_UNPARSED_ARABIC_MINUTES.search(normalized_text.strip())
                or _MALFORMED_NUMERIC_CLOCK.search(normalized_text))


# تحويل عدد دقائق إلى وقت بصيغة HH:MM.
def _format_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


# تحليل التعبير الزمني الذي ذكره المريض.
# جواب فترة مستقل يكمّل ساعة غامضة بدل استبدالها بنافذة بحث جديدة.
def period_reply(message: str) -> str | None:
    text = message.strip().strip(".!؟?").casefold()
    if text in ("morning", "am", "صباح", "صباحا", "صباحًا", "الصباح"):
        return "AM"
    if text in ("afternoon", "evening", "pm", "مساء", "مساءً", "مساءا", "المساء", "بعد الظهر", "العصر"):
        return "PM"
    return None


# تحول عبارة ساعة قصيرة إلى HH:MM للتحليل فقط، دون تغيير transcript أو افتراض AM/PM.
def normalize_clock_words(message: str) -> str:
    words = {name: i for i, name in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
    words.update(ARABIC_HOURS)
    text = re.sub(r"[\u064b-\u065f\u0670]", "", message)
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    # Keep the hour unpadded so the offered list can still resolve AM/PM.
    def quarter_to(match):
        hour = _arabic_relative_hour(match)
        if match["quarter"] and 1 <= hour <= 12:
            return f"{(hour - 2) % 12 + 1}:45"
        return match[0]

    text = _ARABIC_RELATIVE_TIME.sub(quarter_to, text)
    arabic_spoken = (
        rf"(?<!\w)(?P<hour>\d{{1,2}}|{_ARABIC_HOUR_WORDS})\s+و\s*"
        rf"(?:(?P<unit>{_ARABIC_UNITS})\s+و\s*(?P<tens>{_ARABIC_TENS})|(?P<minute>{_ARABIC_MINUTES}|[0-5]?\d))"
        r"\s*(?:دقيقة|دقايق|دقائق)(?!\w)"
    )

    def arabic_spoken_minutes(match):
        hour, minute = match.group("hour", "minute")
        hour_number = int(hour) if hour.isdigit() else ARABIC_HOURS[hour]
        if minute is None:
            minute_number = _ARABIC_MINUTE_UNITS[match["unit"]] + _ARABIC_MINUTE_TENS[match["tens"]]
        else:
            minute_number = int(minute) if minute.isdigit() else _ARABIC_MINUTE_WORDS[minute]
        return f"{hour_number}:{minute_number:02d}"

    text = re.sub(arabic_spoken, arabic_spoken_minutes, text)
    hours = r"(?:\d{1,2}|" + "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)) + r")"
    # Convert spoken English minutes before matching the shorter half-hour forms.
    minutes = dict(zip("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(10, 20)))
    for tens_word, tens in (("twenty", 20), ("thirty", 30), ("forty", 40), ("fifty", 50)):
        minutes[tens_word] = tens
        for unit, value in zip("one two three four five six seven eight nine".split(), range(1, 10)):
            minutes[f"{tens_word} {unit}"] = tens + value
    minute_words = "|".join(re.escape(word).replace(r"\ ", r"[\s-]+")
                            for word in sorted(minutes, key=len, reverse=True))

    def spoken_minutes(match):
        hour = match["hour"].casefold()
        minute = re.sub(r"[\s-]+", " ", match["minute"].casefold())
        return f"{int(hour) if hour.isdigit() else words[hour]}:{minutes[minute]:02d}"

    text = re.sub(rf"(?<!\w)(?P<hour>{hours})\s+(?P<minute>{minute_words})(?!\w)", spoken_minutes, text, flags=re.I)
    # Give offered-slot matching the same clock form for spoken quarter hours.
    quarter = r"و\s*(?:(?:ال)?ربع|خمستعش(?:\s+(?:دقيقة|دقايق|دقائق))?)"
    pattern = rf"(?<!\w)(?P<hour>{hours})\s*(?P<part>{quarter}|and a half|thirty|ونص|والنصف|و نص|ونصف)(?!\w)"
    def replace(match):
        value = match.group("hour").casefold()
        minute = "15" if re.fullmatch(quarter, match.group("part")) else "30"
        return f"{int(value) if value.isdigit() else words[value]}:{minute}"
    return re.sub(pattern, replace, text, flags=re.I)


# تستقبل تفضيل الوقت وتعيد قيودًا صارمة أو حالة غموض دون تخمين.
def normalize_time(
    time_text: str | None,
) -> TimePreference:

    # عدم وجود وقت يعني عدم وجود تفضيل زمني.
    if not time_text or not time_text.strip():
        return _result("missing")
    if has_incomplete_arabic_relative_time(time_text):
        return _result("unrecognized")

    text = normalize_clock_words(time_text.lower().strip())
    if has_malformed_clock(text):
        return _result("unrecognized")
    # أرقام الساعة المنطوقة تبقى خاضعة لفحص AM/PM؛ three o'clock وحدها ليست 3 PM.
    english_hours = {name: index for index, name in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split())}
    text = re.sub(r"\b(?:" + "|".join(english_hours) + r")\b",
                  lambda match: str(english_hours[match.group()]), text)
    text = re.sub(r"\s+o['’]?clock\b", "", text)
    # Join spaced meridiems in transcripts such as "two p m".
    text = re.sub(r"(?<=\d)\s*([ap])\.?\s+m\.?(?!\w)", r" \1m", text)

    # إزالة التشكيل العربي.
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)

    # تحويل الأرقام العربية والفارسية.
    text = text.translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    ))

    # قبول جميع الأوقات إذا صرح المريض بذلك.
    if any(value in text for value in (
        "any time",
        "anytime",
        "no preference",
        "أي وقت",
        "اي وقت",
    )):
        return _result("missing")

    # معرفة هل المريض حدد وقتا بعد أو قبل ساعة.
    # لا نخلط بين "بعد 3" و"بعد الظهر".
    direction_match = re.match(
        r"^(after|before|بعد(?!\s+الظهر)|قبل(?!\s+الظهر))\b",
        text,
    )

    direction = direction_match.group(1) if direction_match else None

    # اكتشاف صباح أو مساء.
    am = (
        bool(re.search(r"\ba\.?m\.?\b", text))
        or any(value in text for value in (
            "morning", "صباح", "الصبح", "قبل الظهر",
        ))
    )

    pm = (
        bool(re.search(r"\bp\.?m\.?\b", text))
        or any(value in text for value in (
            "afternoon", "evening",
            "مساء", "المسا", "بعد الظهر", "العصر",
        ))
    )

    # قراءة ساعة تحتوي على دقائق.
    numeric = re.search(
        r"(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)",
        text,
    )

    if numeric:
        hour = int(numeric.group(1))
        minute = int(numeric.group(2))

        leading_zero = (
            len(numeric.group(1)) == 2
            and numeric.group(1).startswith("0")
        )

    else:
        # قراءة ساعة مع AM/PM أو كلمات عربية.
        numeric = re.search(
            r"(?<!\d)(\d{1,2})\s*"
            r"(?:a\.?m\.?|p\.?m\.?|in\s+(?:the\s+)?(?:morning|afternoon|evening)|صباحا?|مساءا?|بعد\s+(?:الظهر|العصر)|العصر)(?!\w)",
            text,
        )

        # دعم "at 4" و"الساعة 4".
        if numeric is None:
            numeric = re.search(
                r"(?:\bat\s+|الساعة\s+|الساعه\s+)"
                r"(\d{1,2})(?!\d)",
                text,
            )

        # دعم "after 3" و"بعد 3".
        if numeric is None and direction:
            numeric = re.search(
                r"(?:after|before|بعد|قبل)\s+"
                r"(\d{1,2})(?!\d)",
                text,
            )

        if numeric is None:
            numeric = re.fullmatch(r"(\d{1,2})", text)
        hour = int(numeric.group(1)) if numeric else None
        minute = 0
        leading_zero = False

        # دعم نصف الساعة.
        if hour is not None and (
            "ونص" in text or "والنصف" in text
        ):
            minute = 30

    # قراءة الساعة المكتوبة بالكلمات العربية.
    if hour is None:
        for expression, value in sorted(
            ARABIC_HOURS.items(),
            key=lambda item: -len(item[0]),
        ):
            if re.search(
                r"(?<!\w)" + re.escape(expression) + r"(?!\w)",
                text,
            ):
                hour = value

                if "نصف" in text or "ونص" in text:
                    minute = 30

                break

    # أجزاء الساعة المحكية تُطبّق بعد قراءة الرقم أو الكلمة، دون افتراض صباح/مساء.
    if hour is not None and ("وربع" in text or "والربع" in text):
        minute = 15

    # معالجة ساعة واضحة.
    if hour is not None:

        if hour > 23 or minute > 59:
            return _result("unrecognized")

        if am and pm:
            return _result("ambiguous")

        if am or pm:
            if not 1 <= hour <= 12:
                return _result("unrecognized")

            hour = (hour % 12) + (12 if pm else 0)

        elif 1 <= hour <= 12 and not leading_zero:
            # لا نخمن AM أو PM.
            return _result("ambiguous")

        requested_time = f"{hour:02d}:{minute:02d}"
        total_minutes = hour * 60 + minute

        # بعد الساعة يعني بعد الحد نفسه، وليس عنده.
        if direction in ("after", "بعد"):
            if total_minutes == 1439:
                return _result("unrecognized")

            return _result(
                "window",
                after=_format_minutes(total_minutes + 1),
            )

        # قبل الساعة يعني أن الحد الأعلى حصري.
        if direction in ("before", "قبل"):
            if total_minutes == 0:
                return _result("unrecognized")

            return _result(
                "window",
                before=requested_time,
            )

        # دون before أو after، الوقت محدد بالضبط.
        return _result(
            "exact",
            requested_time=requested_time,
        )

    # إذا ذكر قبل أو بعد دون ساعة واضحة نطلب توضيحا.
    if direction:
        return _result("unrecognized")

    # فترة الصباح.
    if any(value in text for value in (
        "morning", "صباح", "الصبح", "قبل الظهر",
    )):
        return _result(
            "window",
            after="09:00",
            before="12:00",
        )

    # فترة بعد الظهر.
    if any(value in text for value in (
        "afternoon", "بعد الظهر",
    )):
        return _result(
            "window",
            after="12:00",
            before="17:00",
        )

    # فترة المساء.
    if any(value in text for value in (
        "evening", "مساء", "العصر",
    )):
        return _result(
            "window",
            after="17:00",
            before="19:00",
        )

    # لا نخمن الوقت غير المفهوم.
    return _result("unrecognized")
