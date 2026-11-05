# تحقق محلي من هوية المريض؛ لا يثبت الهوية قانونيًا ولا يضيف سياسة جديدة لولي الأمر.
import re
import unicodedata


# يقبل أسماء عربية أو لاتينية متعددة الأجزاء، ويرفض الأرقام والأوصاف والبدائل الواضحة.
def valid_patient_name(name: str | None) -> bool:
    if not isinstance(name, str):
        return False
    text = " ".join(name.casefold().split())
    invalid = {"my son", "my daughter", "my child", "my wife", "my husband", "my mother", "my father",
               "ابني", "بنتي", "طفلي", "زوجتي", "زوجي", "امي", "أمي", "ابي", "أبي",
               "my brother", "my sister", "my friend", "my nephew", "my niece", "my grandson", "my granddaughter",
               "اخي", "أخي", "اختي", "أختي", "صديقي", "صديقتي", "اسم تجريبي",
               "unknown", "n/a", "none", "test", "test patient", "full name", "patient name", "اسم المريض"}
    if text in invalid or len(text) > 80 or any(c.isdigit() for c in text):
        return False
    if any(not (c.isalpha() or unicodedata.category(c).startswith("M") or c in " '-’" ) for c in text):
        return False
    return len(text.split()) >= 2 and all(sum(c.isalpha() for c in part) >= 2 for part in text.split())


# نفس الصيغ التي يقبلها عقد العيادة؛ لا نخترع صفرًا أو رمز دولة لرقم ناقص.
def normalize_patient_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    text = re.sub(r"[\s\-.()/]", "", text)
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("00"):
        text = text[2:]
    for country in ("970", "972"):
        if text.startswith(country) and len(text) == 12:
            text = "0" + text[3:]
    return text if re.fullmatch(r"0\d{8,9}", text) else None


# دليل هاتف صريح وحيد في الرسالة يحافظ على الصفر الذي كتبه المريض إن أسقطه LLM.
def explicit_phone(message: str) -> str | None:
    candidates = re.findall(r"(?<![\w])(?:\+|00)?\d[\d\s()./-]{7,}\d(?!\w)", message)
    phones = {phone for value in candidates if (phone := normalize_patient_phone(value))}
    return next(iter(phones)) if len(phones) == 1 else None


# يفك تسلسل أرقام منطوقًا حرفيًا فقط؛ لا يحول مئات أو تخمينات إلى هاتف.
def spoken_phone_digits(message: str) -> str | None:
    text = message.casefold().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    text = re.sub(r"^(?:my (?:phone(?: number)?|number)(?: is)?|phone(?: number)?(?: is)?|رقم (?:تلفوني|هاتفي)(?: هو)?|رقمي(?: هو)?|تلفوني)\s*[:،]?\s*", "", text.strip())
    words = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
    words.update({"oh": "0", "صفر": "0", "واحد": "1", "واحدة": "1", "اثنين": "2", "اتنين": "2", "تنين": "2",
                  "ثلاثة": "3", "تلاتة": "3", "أربعة": "4", "اربعة": "4", "خمسة": "5", "ستة": "6", "سبعة": "7",
                  "ثمانية": "8", "تمانية": "8", "تسعة": "9", "تسع": "9"})
    tokens = re.findall(r"\w+|\+", text)
    if not tokens or re.search(r"[^\w\s+,،.()/-]", text):
        return None
    if all(token.isdecimal() or token in words for token in tokens):
        return "".join(token if token.isdecimal() else words[token] for token in tokens)
    return None


# سبب رفض محدد وفق norm_phone الأصلية؛ صحة الشكل لا تعني دقة STT أو ملكية الرقم.
def phone_validation_issue(value: str | None) -> str | None:
    if normalize_patient_phone(value):
        return None
    if not isinstance(value, str):
        return "format"
    text = re.sub(r"[\s\-.()/]", "", value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")))
    text = text[1:] if text.startswith("+") else text
    text = text[2:] if text.startswith("00") else text
    if not text.isdecimal():
        return "format"
    if text.startswith(("970", "972")):
        return "length" if len(text) != 12 else "prefix"
    return "length" if len(text) not in (9, 10) else "prefix"


# ضجيج اسم بعد اختيار موعد لا يُعد طلب تغيير موعد؛ الاختيار الصريح يبقى متاحًا.
def patient_name_noise(state: dict, message: str) -> bool:
    return bool(state.get("selected_slot_id") and not valid_patient_name(state.get("patient_name"))
                and (re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", message) or spoken_phone_digits(message) is not None))


# هذه ليست موافقة حجز؛ جواب واضح على سؤال تأكيد الهاتف وحده.
def phone_confirmation_reply(message: str) -> bool | None:
    text = " ".join(re.findall(r"\w+", message.casefold()))
    if text in ("yes", "correct", "that s correct", "yes that phone number is correct", "yes that number is correct", "the number is correct", "نعم", "صح", "صحيح", "الرقم صحيح", "نعم الرقم صحيح", "نعم هذا الرقم صحيح"):
        return True
    if text in ("no", "incorrect", "wrong", "لا", "الرقم خطأ", "غلط"):
        return False
    return None


# لا ننفذ مسار القاصر نيابة عن معمارية A؛ نوقف الموافقة حتى التحقق الموجود بالمشروع.
def third_party_request(message: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:my (?:son|daughter|child)|ابني|بنتي|طفلي)[.!؟?]?\s*", message, re.I) or re.search(r"\b(?:for\s+my\s+(?:son|daughter|child|wife|husband|mother|father)|another patient|someone else)\b|لابني|لبنتي|لطفلي|لزوجتي|لزوجي|لأمي|لامي|لأبي|لابي|لمريض آخر|لشخص (?:آخر|تاني)", message, re.I))


# تبديل المريض يبطل الهوية والموافقة والاختيار؛ تفاصيل بحث الموعد يمكن مراجعتها من جديد.
def clear_patient(state: dict) -> dict:
    return {**state, "patient_name": None, "patient_phone": None, "patient_lookup_phone": None,
            "confirmed_phone": None, "phone_candidate": None, "phone_error_reason": None,
            "patient_lookup_status": "not_checked", "preferred_provider_id": None,
            "selected_slot_id": None, "pending_action": None, "selection_candidates": []}
