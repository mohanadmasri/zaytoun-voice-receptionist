
# ==================================================
# تذكير — dates.py
#
# normalize_date تحول التاريخ الذي قاله المريض إلى YYYY-MM-DD.
# تعتمد على تاريخ العيادة وليس تاريخ جهاز المستخدم.
# تدعم بعض التعبيرات العربية والإنجليزية والتواريخ المباشرة.
# تعيد None عندما لا تستطيع فهم التاريخ بأمان.
# لا تستدعي LLM أو MCP بنفسها.
# ==================================================

import re
from datetime import date, timedelta


# تحول تعبير المريض عن التاريخ إلى تاريخ واضح اعتمادًا على يوم العيادة.
def normalize_date(
    date_text: str | None,
    clinic_today: str
) -> str | None:

    # عدم محاولة تفسير قيمة فارغة
    if not date_text or not date_text.strip():
        return None

    # تحويل تاريخ العيادة من نص إلى كائن date
    today = date.fromisoformat(clinic_today)

    # تنظيف النص وإزالة التشكيل العربي والمسافات الزائدة
    text = date_text.strip().lower()
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = " ".join(text.strip(".,!?؟،").split())

    # معالجة اليوم والغد وبعد الغد
    if text in ("today", "اليوم"):
        return today.isoformat()

    if text in ("tomorrow", "بكرا", "بكرة", "غدا"):
        return (today + timedelta(days=1)).isoformat()

    if text in ("day after tomorrow", "بعد بكرا", "بعد بكرة", "بعد غد"):
        return (today + timedelta(days=2)).isoformat()

    # قبول التاريخ المباشر إذا كان صحيحًا وليس في الماضي
    try:
        exact_date = date.fromisoformat(text)
        return exact_date.isoformat() if exact_date >= today else None
    except ValueError:
        pass

    # ربط أسماء أيام الأسبوع الإنجليزية بأرقام Python
    english_days = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    # ربط أسماء أيام الأسبوع العربية بنفس الأرقام
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

    # إزالة كلمات مثل next وthis ويوم والقادم
    day_text = re.sub(r"^(next|this)\s+", "", text)
    day_text = re.sub(r"^يوم\s+", "", day_text)
    day_text = re.sub(
        r"\s+(القادم|القادمة|الجاي|الجاية)$",
        "",
        day_text
    )

    # معرفة رقم يوم الأسبوع المطلوب
    target_day = english_days.get(day_text)

    if target_day is None:
        target_day = arabic_days.get(day_text)

    # إذا لم نتعرف على اليوم، لا نخمن
    if target_day is None:
        return None

    # حساب عدد الأيام حتى الموعد القادم لهذا اليوم
    days_ahead = (target_day - today.weekday()) % 7

    # إذا كان اليوم نفسه، نختار الأسبوع القادم
    if days_ahead == 0:
        days_ahead = 7

    # إعادة التاريخ النهائي بصيغة YYYY-MM-DD
    return (today + timedelta(days=days_ahead)).isoformat()
