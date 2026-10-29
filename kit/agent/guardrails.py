
# ==================================================
# تذكير — guardrails.py
#
# الهدف: حماية مسار الحجز من الطلبات غير الآمنة.
#
# 1. نفحص رسالة المريض قبل تنفيذ إجراءات الحجز.
# 2. نكتشف بعض مؤشرات الطوارئ الطبية.
# 3. نرفض إعطاء التشخيصات والنصائح الدوائية.
# 4. نكتشف بعض محاولات تجاوز تعليمات النظام.
# 5. نقلل الإنذارات الخاطئة الناتجة عن النفي والاقتباس.
# 6. نعيد قرارا واضحا مع رد مناسب عند الحاجة.
#
# هذا الملف لا يستخدم LLM ولا MCP ولا ينشئ حجوزات.
# القواعد النصية محدودة، وليست ضمانا لاكتشاف كل خطر.
# ==================================================

import re
import unicodedata
from typing import Literal, TypedDict


class GuardrailDecision(TypedDict):
    kind: Literal["allow", "emergency", "medical_advice", "injection"]
    reply: str | None


# توحيد النص لتسهيل المقارنة بالعربية والإنجليزية.
def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()

    text = "".join(
        char for char in text
        if unicodedata.category(char) != "Mn" and char != "ـ"
    )

    for letter in ("أ", "إ", "آ", "ٱ"):
        text = text.replace(letter, "ا")

    return " ".join(text.split())


# أنماط أولية تشير إلى أعراض طارئة.
EMERGENCY_PATTERNS = (
    r"\b(?:severe|strong|crushing|intense)?\s*chest\s+pain\b",
    r"\b(?:cannot|can't|can not|struggling to|trouble|difficulty)\s+(?:breathe|breathing)\b",
    r"\b(?:unconscious|not breathing|severe bleeding|passed out)\b",
    r"(?:الم|وجع|ضغط)\s*(?:قوي|شديد)?\s*(?:في\s*|ب)?\s*(?:ال)?صدر",
    r"صعوبه\s*(?:في\s*)?التنفس",
    r"(?:مش قادر|ما بقدر|لا استطيع)\s+اتنفس",
    r"اختناق|فاقد الوعي|نزيف شديد",
)


# محاولات واضحة لتجاوز قواعد الحجز والموافقة.
INJECTION_PATTERNS = (
    r"\bignore (?:all )?(?:your |the )?(?:previous |system )?(?:rules|instructions)\b",
    r"\b(?:no confirmation needed|without (?:any )?confirmation|skip (?:the )?approval)\b",
    r"\bbook (?:every|all) (?:free )?slots?\b",
    r"(?:تجاهل|انس|تخطى).*(?:التعليمات|القواعد|الموافقه)",
    r"احجز\s+(?:كل|جميع)\s+(?:المواعيد|الاوقات)",
)


# أسئلة تطلب تشخيصا أو وصف علاج بدلا من حجز موعد.
MEDICAL_ADVICE_PATTERNS = (
    r"\b(?:what|which) (?:medicine|medication|drug)\b",
    r"\b(?:diagnose me|give me a diagnosis|what disease do i have)\b",
    r"(?:اي|شو|ما هو)\s+(?:دواء|الدوا|العلاج)",
    r"شخصني|شخص حالتي|اعطني دواء",
)


# التحقق من وجود واحد من الأنماط في الرسالة.
def matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


# ==================================================
# تذكير — check_guardrails
#
# 1. توحيد نص رسالة المريض.
# 2. تقليل بعض الإنذارات الخاطئة.
# 3. فحص الطوارئ أولا لأنها الأعلى أولوية.
# 4. فحص محاولات تجاوز التعليمات.
# 5. فحص طلبات التشخيص والأدوية.
# 6. إعادة allow إذا لم نكتشف مشكلة.
#
# هذه الدالة لا تعدل AgentState.
# ==================================================

def check_guardrails(
    message: str,
    language: str = "unknown"
) -> GuardrailDecision:

    # توحيد النص.
    text = normalize_text(message)

    # ==================================================
    # تذكير — معالجة النفي
    #
    # لا نريد اعتبار:
    # "I do not have chest pain"
    # حالة طوارئ لمجرد وجود عبارة chest pain.
    #
    # نزيل بعض صيغ النفي المعروفة من نسخة فحص الطوارئ.
    # نبقي النص الأصلي لبقية فحوصات الحماية.
    # ==================================================

    emergency_text = re.sub(
        r"\b(?:i\s+)?(?:do not|don't|dont)\s+have\s+"
        r"(?:any\s+)?(?:severe\s+|strong\s+|crushing\s+|intense\s+)?"
        r"chest\s+pain\b"
        r"|\bno\s+(?:severe\s+|strong\s+|crushing\s+|intense\s+)?"
        r"chest\s+pain\b",
        " ",
        text,
    )

    # ==================================================
    # تذكير — معالجة الاقتباسات
    #
    # نميز بين محاولة تنفيذ تعليمات خبيثة
    # وبين سؤال بسيط عن معنى عبارة مقتبسة.
    #
    # نستخدم fullmatch حتى لا نتجاهل أوامر إضافية
    # تأتي خارج السؤال نفسه.
    # ==================================================

    quoted_explanation = bool(
        re.fullmatch(
            r"""(?:can you explain what|what does)\s+"""
            r"""(['"])(.+?)\1\s+means?\??""",
            text,
        )
    )

    # تحديد لغة الرد، حتى لو لم يعمل Intake بعد.
    is_arabic = language == "ar" or (
        language != "en"
        and bool(re.search(r"[\u0600-\u06FF]", message))
    )

    # ==================================================
    # الفحص الأول — الطوارئ الطبية
    # نستخدم النسخة التي عالجنا فيها بعض صيغ النفي.
    # ==================================================

    if matches_any(emergency_text, EMERGENCY_PATTERNS):
        return {
            "kind": "emergency",
            "reply": (
                "قد تكون هذه حالة طارئة. اتصل على 101 الآن "
                "أو توجه إلى أقرب قسم طوارئ. "
                "لا أستطيع تقديم تشخيص أو نصيحة دوائية."
                if is_arabic
                else
                "This may be a medical emergency. "
                "Call 101 now or go to the nearest emergency department. "
                "I cannot provide a diagnosis or medication advice."
            ),
        }

    # ==================================================
    # الفحص الثاني — محاولات التلاعب
    # نستثني السؤال البسيط عن معنى عبارة مقتبسة.
    # ==================================================

    if (
        not quoted_explanation
        and matches_any(text, INJECTION_PATTERNS)
    ):
        return {
            "kind": "injection",
            "reply": (
                "يمكنني مساعدتك في حجز موعد واحد، "
                "لكن لا يمكن تجاوز قواعد الحجز أو الموافقة."
                if is_arabic
                else
                "I can help with one appointment, "
                "but booking and approval rules cannot be bypassed."
            ),
        }

    # ==================================================
    # الفحص الثالث — طلب التشخيص أو النصائح الدوائية
    # ==================================================

    if matches_any(text, MEDICAL_ADVICE_PATTERNS):
        return {
            "kind": "medical_advice",
            "reply": (
                "لا أستطيع تقديم تشخيص أو وصف دواء. "
                "يمكنني مساعدتك في حجز استشارة طبية."
                if is_arabic
                else
                "I cannot provide a diagnosis or recommend medication. "
                "I can help you book a medical consultation."
            ),
        }

    # لم تكتشف القواعد أي مشكلة.
    return {
        "kind": "allow",
        "reply": None,
    }
