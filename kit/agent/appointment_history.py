"""Read-only appointment history for the current conversation."""

import re

from agent.presentation import display_date, display_time, say

LATEST = {
    "شو اخر موعد حجزته", "ما اخر موعد حجزته",
    "what did i book last", "what was my last booking", "what is my latest booking",
}
ALL = {
    "شو مواعيدي", "شو مواعيدي المحجوزه", "شو المواعيد اللي انا عندي فيها موعد",
    "شو المواعيد المحجوزه لي", "شو المواعيد المحجوزه الي", "شو المواعيد اللي حجزتها",
    "عندي حجز", "ورجيني حجوزاتي", "ما هي مواعيدي",
    "what are my appointments", "show my bookings", "show my appointments",
    "do i have a booking", "do i have any appointments",
    "what appointments have i booked", "what appointments are booked for me",
}


def history_query(message: str) -> str | None:
    text = re.sub(r"[\u0640\u064b-\u065f]", "", message.casefold())
    text = text.translate(str.maketrans("أإآة", "اااه"))
    text = " ".join(re.findall(r"\w+", text))
    if text in LATEST:
        return "latest"
    if text in ALL:
        return "all"
    return None


def confirmed_change_request(message: str) -> bool:
    text = re.sub(r"[\u0640\u064b-\u065f]", "", message.casefold())
    return bool(re.search(
        r"(?:[أا]?غير|نغير|تغيير)\s+(?:المواعيد|الحجوزات)\s+المحجوز[ةه]"
        r"|(?:[أا]?غير|نغير|تغيير)\s+(?:موعد(?:ي)?|الموعد|هالموعد|الحجز)(?!\w)"
        r"|\breschedul(?:e|ing)\b|\b(?:change|move)\s+(?:(?:my|the|this|that)\s+)?"
        r"(?:(?:confirmed|booked)\s+)?(?:appointment|booking)s?\b",
        text,
    ))


def confirmed_change_reply(records: list[dict], message: str, language: str) -> str:
    context = {"language": language}
    confirmed = [row for row in records if row["status"] == "confirmed"]
    references = {reference.upper() for reference in re.findall(r"\bBK-[A-Z0-9-]+\b", message, re.I)}
    if references:
        confirmed = [row for row in confirmed if row["booking_id"].upper() in references]
    if not confirmed:
        return say(context, "I can't identify a confirmed booking for that request in this conversation. Please contact the clinic to verify it.",
                   "ما قدرت أحدد حجز مؤكد لهالطلب بهالمحادثة. تواصل مع العيادة للتحقق منه.")
    if len(confirmed) > 1:
        return say(context, "Which confirmed appointment do you mean? Please give its booking reference. The clinic must make the change.",
                   "أي موعد مؤكد بتقصد؟ احكي رقم الحجز. تغيير الموعد لازم يتم عن طريق العيادة.")
    return history_reply(confirmed, "selected", language) + "\n" + say(
        context, "Please contact the clinic to change the confirmed appointment. No change or new booking was made.",
        "تواصل مع العيادة لتغيير الموعد المؤكد، لو سمحت. ما تغيّر الموعد وما انعمل حجز جديد.")


def history_reply(records: list[dict], query: str, language: str) -> str:
    context = {"language": language}
    if not records:
        return say(context,
                   "There are no bookings in this conversation. Please contact the clinic to verify your identity and check bookings from another conversation.",
                   "ما في حجوزات بهالمحادثة. للمواعيد من محادثة تانية، تواصل مع العيادة حتى تتحقق من هويتك.")
    rows = []
    for record in reversed(records[-1:] if query == "latest" else records):
        status = (say(context, "Confirmed", "مؤكد") if record["status"] == "confirmed"
                  else say(context, "Cancelled", "ملغى"))
        service = say(context, record.get("service_name"), record.get("service_name_ar")) or record.get("service_name")
        doctor = say(context, record.get("provider_name"), record.get("provider_name_ar")) or record.get("provider_name")
        day = display_date(record["date"], language) if record.get("date") else None
        clock = display_time(record["start"], language) if record.get("start") else None
        rows.append(" · ".join(value for value in (status, service, doctor, day, clock, record["booking_id"]) if value))
    heading = (say(context, "Your latest booking in this conversation", "آخر موعد حجزته بهالمحادثة")
               if query == "latest" else say(context, "Your bookings in this conversation", "مواعيدك بهالمحادثة"))
    if query == "selected":
        heading = say(context, "The confirmed appointment you asked about", "الموعد المؤكد اللي سألت عنه")
    return "\n".join([heading, *rows])
