# عرض معلومات العيادة للمريض بالإنجليزية أو العربية دون كشف معرفات الأدوات الداخلية.
# المدخل حالة الجلسة وبيانات الكتالوج الموثقة؛ المخرج نص مناسب للعرض والنطق.
# لا يستخدم LLM أو MCP ولا يغير تفاصيل الموعد أو صلاحيات الموافقة.
from datetime import date


# تختار نص الرد من لغة الجلسة؛ الأرقام والرسائل القصيرة تحتفظ باللغة السابقة.
def say(state: dict, english: str, arabic: str) -> str:
    return arabic if state.get("language") == "ar" else english


# تعرض التاريخ مرة واحدة بصيغة مقروءة؛ لا تعتمد على locale أو تاريخ الجهاز.
def display_date(value: str, language: str = "en") -> str:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "التاريخ المختار" if language == "ar" else "the selected date"
    months = ("يناير فبراير مارس أبريل مايو يونيو يوليو أغسطس سبتمبر أكتوبر نوفمبر ديسمبر".split()
              if language == "ar" else
              "January February March April May June July August September October November December".split())
    return (f"{day.day} {months[day.month - 1]} {day.year}" if language == "ar"
            else f"{months[day.month - 1]} {day.day}, {day.year}")


# تحول HH:MM إلى صيغة 12 ساعة؛ هذه للعرض فقط ولا تدخل في المطابقة أو MCP.
def display_time(value: str, language: str = "en") -> str:
    hour, minute = map(int, value.split(":"))
    period = ("صباحًا" if hour < 12 else "مساءً") if language == "ar" else ("AM" if hour < 12 else "PM")
    return f"{hour % 12 or 12}:{minute:02d} {period}"


# تختار اسم الطبيب من كتالوج العيادة أو بيانات الموعد؛ لا تعرض provider_id كبديل للاسم.
def provider_label(slot: dict, state: dict) -> str:
    provider = next((p for p in state.get("provider_catalog", []) if p.get("id") == slot.get("provider_id")), {})
    if state.get("language") == "ar":
        return slot.get("provider_name_ar") or provider.get("name_ar") or slot.get("provider_name") or "الطبيب"
    return slot.get("provider_name") or provider.get("name") or "the doctor"


# تعرض اسم الخدمة الرسمي من الكتالوج؛ لا تستخدم service_id كنص للمريض.
def service_label(state: dict) -> str:
    service = next((s for s in state.get("service_catalog", []) if s.get("id") == state.get("service_id")), {})
    return (service.get("name_ar") or "الموعد") if state.get("language") == "ar" else (service.get("name_en") or "the appointment")


# تبني ملخص الموافقة من نفس الموعد المتحقق منه دون تغيير payload أو توليد معرفات.
def booking_summary(state: dict, slot: dict) -> str:
    language = state.get("language", "en")
    day, time, doctor = display_date(slot["date"], language), display_time(slot["start"], language), provider_label(slot, state)
    return say(state, f"Book {service_label(state)} on {day} at {time} with {doctor} for {state['patient_name']} ({state['patient_phone']}).",
               f"حجز {service_label(state)} يوم {day} الساعة {time} مع {doctor} باسم {state['patient_name']} ({state['patient_phone']}).")
