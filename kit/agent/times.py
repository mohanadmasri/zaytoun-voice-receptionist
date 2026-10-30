
# times.py
# هذا الملف يحلل الوقت الذي يذكره المريض.
# يميز بين الساعة المحددة والفترة الزمنية.
# يدعم العربية والإنجليزية ولا ينشئ أي حجز.

import re
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
}


# تحويل عدد دقائق إلى وقت بصيغة HH:MM.
def _format_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


# تحليل التعبير الزمني الذي ذكره المريض.
def normalize_time(
    time_text: str | None,
) -> TimePreference:

    # عدم وجود وقت يعني عدم وجود تفضيل زمني.
    if not time_text or not time_text.strip():
        return _result("missing")

    text = time_text.lower().strip()

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
            "مساء", "بعد الظهر", "العصر",
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
            r"(?:a\.?m\.?|p\.?m\.?|صباحا?|مساءا?)(?!\w)",
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
