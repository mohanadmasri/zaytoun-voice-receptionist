
# ==================================================
# تذكير — times.py
#
# الهدف: فهم الوقت الذي يذكره المريض.
#
# exact      → ساعة محددة.
# window     → فترة زمنية مثل الصباح.
# missing    → لم يحدد المريض وقتا، أو يقبل أي وقت.
# ambiguous  → الوقت يحتاج توضيحا.
# unrecognized → تعبير لم نفهمه.
#
# لا يبحث هذا الملف عن المواعيد ولا يحجزها.
# ==================================================

import re
from typing import Literal, TypedDict


# شكل نتيجة تحليل الوقت.
class TimePreference(TypedDict):
    kind: Literal[
        "exact", "window", "missing",
        "ambiguous", "unrecognized"
    ]

    requested_time: str | None
    after: str | None
    before: str | None


# تجهيز النتيجة دون تكرار القاموس في كل شرط.
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


# أسماء الساعات المكتوبة بالعربية.
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


# الدالة الرئيسية لتحليل الوقت.
def normalize_time(
    time_text: str | None
) -> TimePreference:

    # 1. لم يذكر المريض وقتا.
    if not time_text or not time_text.strip():
        return _result("missing")

    text = time_text.lower().strip()

    # إزالة التشكيل العربي.
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)

    # تحويل الأرقام العربية والفارسية إلى أرقام إنجليزية.
    text = text.translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789"
    ))

    # 2. المريض يقبل أي وقت.
    if any(word in text for word in (
        "any time", "anytime", "no preference",
        "أي وقت", "اي وقت",
    )):
        return _result("missing")

    # 3. معرفة هل الوقت صباحي أو مسائي.
    am = (
        bool(re.search(r"\ba\.?m\.?\b", text))
        or any(word in text for word in (
            "morning", "صباح", "الصبح", "قبل الظهر"
        ))
    )

    pm = (
        bool(re.search(r"\bp\.?m\.?\b", text))
        or any(word in text for word in (
            "afternoon", "evening",
            "مساء", "بعد الظهر", "العصر"
        ))
    )

    # 4. محاولة قراءة ساعة رقمية مثل 4:30 أو 16:30.
    numeric = re.search(
        r"(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)",
        text
    )

    if numeric:
        hour = int(numeric.group(1))
        minute = int(numeric.group(2))

        # 09:30 تعتبر صيغة 24 ساعة واضحة.
        leading_zero = (
            len(numeric.group(1)) == 2
            and numeric.group(1).startswith("0")
        )

    else:
        # دعم 4 PM أو الساعة 4 أو at 4.
        numeric = re.search(
            r"(?<!\d)(\d{1,2})\s*"
            r"(?:a\.?m\.?|p\.?m\.?|صباحا?|مساءا?)(?!\w)",
            text
        ) or re.search(
            r"(?:\bat\s+|الساعة\s+|الساعه\s+)"
            r"(\d{1,2})(?!\d)",
            text
        )

        hour = int(numeric.group(1)) if numeric else None
        minute = 0
        leading_zero = False

        # دعم الساعة 4 ونص.
        if hour is not None and (
            "ونص" in text or "والنصف" in text
        ):
            minute = 30

    # 5. قراءة الساعة المكتوبة بالكلمات العربية.
    if hour is None:
        for expression, value in sorted(
            ARABIC_HOURS.items(),
            key=lambda item: -len(item[0])
        ):
            if re.search(
                r"(?<!\w)" + re.escape(expression) + r"(?!\w)",
                text
            ):
                hour = value

                if "نصف" in text or "ونص" in text:
                    minute = 30

                break

    # 6. لدينا ساعة محددة.
    if hour is not None:

        # رفض ساعة أو دقائق غير صحيحة.
        if hour > 23 or minute > 59:
            return _result("unrecognized")

        # لا يمكن الجمع بين AM و PM.
        if am and pm:
            return _result("ambiguous")

        if am or pm:

            # صيغة AM/PM تستخدم ساعات من 1 إلى 12.
            if hour < 1 or hour > 12:
                return _result("unrecognized")

            # تحويل 12-hour إلى 24-hour.
            hour = (hour % 12) + (12 if pm else 0)

        elif 1 <= hour <= 12 and not leading_zero:

            # مثال: 4:30 وحدها قد تكون صباحا أو مساء.
            # لا نخمن الوقت.
            return _result("ambiguous")

        return _result(
            "exact",
            requested_time=f"{hour:02d}:{minute:02d}"
        )

    # 7. لم يذكر ساعة محددة، لكن حدد فترة.
    if any(word in text for word in (
        "morning", "صباح", "الصبح", "قبل الظهر"
    )):
        return _result(
            "window", after="09:00", before="12:00"
        )

    if any(word in text for word in (
        "afternoon", "بعد الظهر"
    )):
        return _result(
            "window", after="12:00", before="17:00"
        )

    if any(word in text for word in (
        "evening", "مساء", "العصر"
    )):
        return _result(
            "window", after="17:00", before="19:00"
        )

    # 8. تعبير غير مفهوم.
    # لا نخمن ولا نختار موعدا عشوائيا.
    return _result("unrecognized")
