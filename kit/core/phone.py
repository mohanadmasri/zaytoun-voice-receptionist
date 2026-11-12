"""Phone formats supported by the receptionist and simulated clinic."""

import re


DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
MOBILE_PREFIXES = frozenset({"050", "051", "052", "053", "054", "055", "056", "058", "059"})


def _domestic_digits(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\s.()-]", "", value.translate(DIGIT_TRANSLATION))
    if not re.fullmatch(r"\+?[0-9]+", text):
        return None
    if text.startswith(("+", "00")):
        text = text[1:] if text.startswith("+") else text[2:]
        if not text.startswith(("970", "972")):
            return None
    if text.startswith(("970", "972")):
        national = text[3:]
        if not national or national.startswith("0"):
            return None
        return "0" + national
    return text


def phone_validation_issue(value: str | None) -> str | None:
    digits = _domestic_digits(value)
    if digits is None:
        return "format"
    if digits[:3] in MOBILE_PREFIXES or re.match(r"07[2-9]", digits):
        length = 10
    elif re.match(r"0[23489]", digits):
        length = 9
    else:
        return "prefix"
    return None if len(digits) == length else "length"


def normalize_patient_phone(value: str | None) -> str | None:
    # Convert the country code to the domestic trunk prefix without changing subscriber digits.
    return _domestic_digits(value) if phone_validation_issue(value) is None else None
