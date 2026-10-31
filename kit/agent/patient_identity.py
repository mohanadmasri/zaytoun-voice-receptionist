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
    if text in invalid or len(text) > 120 or any(c.isdigit() for c in text):
        return False
    if any(not (c.isalpha() or unicodedata.category(c).startswith("M") or c in " '-’" ) for c in text):
        return False
    return len(text.split()) >= 2 and all(sum(c.isalpha() for c in part) >= 2 for part in text.split())


# لا ننفذ مسار القاصر نيابة عن معمارية A؛ نوقف الموافقة حتى التحقق الموجود بالمشروع.
def third_party_request(message: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:my (?:son|daughter|child)|ابني|بنتي|طفلي)[.!؟?]?\s*", message, re.I) or re.search(r"\b(?:for\s+my\s+(?:son|daughter|child|wife|husband|mother|father)|another patient|someone else)\b|لابني|لبنتي|لطفلي|لزوجتي|لزوجي|لأمي|لامي|لأبي|لابي|لمريض آخر|لشخص (?:آخر|تاني)", message, re.I))


# تبديل المريض يبطل الهوية والموافقة والاختيار؛ تفاصيل بحث الموعد يمكن مراجعتها من جديد.
def clear_patient(state: dict) -> dict:
    return {**state, "patient_name": None, "patient_phone": None, "patient_lookup_phone": None,
            "patient_lookup_status": "not_checked", "preferred_provider_id": None,
            "selected_slot_id": None, "pending_action": None, "selection_candidates": []}
