import re

from agent.patient_identity import valid_patient_name


_NAME_DECLARATION = re.compile(
    r"^\s*(?:no[,.]?\s+|لا[\s،,]+|مش\s+[^،,]+[،,]\s*)?"
    r"(?:my (?:correct )?name is\b|الاسم الصحيح(?:\s+هو)?(?=\s|[:،,]|$)|"
    r"[اإ]سمي(?=\s|[:،,]|$))[\s:،,]*",
    re.IGNORECASE,
)
_I_MEANT = re.compile(r"^\s*(?:no[,.]?\s+)?I meant\b[\s:,]*", re.IGNORECASE)
_PHONE_SUFFIX = re.compile(
    r"(?:\s*,\s*|\s+and\s+)(?:my\s+)?(?:phone|number)\b|"
    r"(?:\s*[،,]\s*|\s+)(?:ورقمي\b|ورقم\s)",
    re.IGNORECASE,
)


def patient_name_statement(message: str, *, standalone: bool = False) -> str | None:
    declaration = _NAME_DECLARATION.match(message)
    if not declaration:
        return None
    parts = _PHONE_SUFFIX.split(message[declaration.end():], maxsplit=1)
    if standalone and len(parts) > 1:
        return None
    return parts[0].strip().rstrip(".!?؟،,").rstrip()


def declared_patient_name(message: str, current_name: str | None, extracted_name: str | None = None) -> str | None:
    name = patient_name_statement(message)
    if name is not None:
        return name if valid_patient_name(name) else None

    # "I meant" can also introduce a date, service, or time correction.
    correction = _I_MEANT.match(message)
    if correction and current_name:
        name = message[correction.end():].strip().rstrip(".!?").rstrip()
        if literal_patient_name(name, extracted_name) == name:
            return name
    return None


def literal_patient_name(message: str, extracted_name: str | None) -> str | None:
    if not valid_patient_name(extracted_name):
        return None
    words = [re.escape(word) for word in extracted_name.split()]
    match = re.search(r"(?<![\w'’\-])" + r"\s+".join(words) + r"(?![\w'’\-])", message, re.IGNORECASE)
    return match.group() if match else None
