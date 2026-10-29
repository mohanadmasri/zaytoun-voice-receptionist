
# time_normalization.py
# This file converts patient time preferences into clinic search boundaries.
# It supports 24-hour times, AM/PM, and common Arabic and English expressions.

import re


# Convert a clock time into HH:MM format.
def parse_clock_time(value: str) -> str | None:
    match = re.fullmatch(
        r"(\d{1,2})(?::([0-5]\d))?\s*(am|pm|صباحا|صباحًا|مساء|مساءً|العصر)?",
        value.strip(),
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    period = match.group(3)

    if period:
        if not 1 <= hour <= 12:
            return None

        if period in ("pm", "مساء", "مساءً", "العصر"):
            hour = (hour % 12) + 12
        else:
            hour = hour % 12
    elif not 0 <= hour <= 23:
        return None

    return f"{hour:02d}:{minute:02d}"


# Convert a time expression into after/before search boundaries.
def normalize_time(time_text: str | None) -> dict | None:
    if not time_text:
        return None

    text = time_text.strip().lower().replace("الساعة", "").strip()

    # Handle common time-of-day preferences.
    periods = {
        "morning": {"after": None, "before": "12:00"},
        "الصبح": {"after": None, "before": "12:00"},
        "صباحا": {"after": None, "before": "12:00"},
        "صباحًا": {"after": None, "before": "12:00"},
        "afternoon": {"after": "12:00", "before": "17:00"},
        "بعد الظهر": {"after": "12:00", "before": "17:00"},
        "evening": {"after": "17:00", "before": None},
        "المساء": {"after": "17:00", "before": None},
    }

    if text in periods:
        return periods[text].copy()

    # Extract the search direction and requested time.
    match = re.fullmatch(
        r"(after|before|بعد|قبل)\s+(.+)",
        text,
    )

    if not match:
        return None

    direction = match.group(1)
    raw_time = match.group(2).strip()
    time_value = parse_clock_time(raw_time)

    if time_value is None:
        return None

    if direction in ("after", "بعد"):
        return {"after": time_value, "before": None}

    return {"after": None, "before": time_value}
