"""Short-lived speech tickets for public clinic replies and fixed private-stage prompts."""

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import re
from time import monotonic
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agent.presentation import display_time, say
from agent.service_mapping import service_question
from agent.slot_selection import format_slot_options

MODEL = "google/gemini-3.8-flash-lite-tts"
VOICE = "Callirrhoe"
PCM_RATE = 24000
MAX_AUDIO_BYTES = PCM_RATE * 2 * 90
DATA = Path(__file__).resolve().parents[1] / "services" / "data"
SERVICES = {row["id"]: row for row in json.loads((DATA / "services.json").read_text(encoding="utf-8"))}
PROVIDERS = {row["id"]: row for row in json.loads((DATA / "providers.json").read_text(encoding="utf-8"))}
PUBLIC_PROMPTS = {
    "This may be a medical emergency. Call 101 now or go to the nearest emergency department.",
    "قد تكون هذه حالة طارئة. اتصل على 101 الآن أو توجه إلى أقرب قسم طوارئ.",
    "Please seek prompt medical evaluation. I cannot diagnose your condition.",
    "يرجى طلب تقييم طبي عاجل. لا أستطيع تشخيص حالتك.",
    "I cannot provide a diagnosis or medication advice, but I can help you book a consultation.",
    "لا أستطيع تقديم تشخيص أو نصيحة دوائية، لكن يمكنني مساعدتك في حجز استشارة.",
    "Your request was rejected. No booking was made.",
    "تم إلغاء الطلب، وما تم أي حجز. بتحب نبحث عن موعد تاني؟",
    "Cancellation rejected. Nothing was cancelled.",
    "رفضت الإلغاء، وما انلغى أي موعد.",
    "Review the transcript. If correct, select Use transcript; otherwise record again.",
    "راجع النص اللي سمعته. إذا صحيح اضغط «استخدم النص»، أو أعد التسجيل.",
    "The clinic is closed on your requested day and no later appointments are available.",
    "العيادة مسكّرة باليوم المطلوب وما في مواعيد متاحة بعده.",
    "That time is outside clinic opening hours.", "هالساعة خارج دوام العيادة.",
    "That time is outside clinic opening hours. Please choose another time.",
    "هالساعة خارج دوام العيادة. اختار ساعة تانية، لو سمحت.",
    "That time has passed. Which later time works for you?", "هالساعة مرقت. أي ساعة بعدها بتناسبك؟",
    "That time has passed. Please choose a later appointment.", "هالساعة مرقت. اختار موعد بعد هالوقت، لو سمحت.",
    "Those appointments have passed. Which upcoming time works for you?", "هالمواعيد مرقت. أي وقت جاي بناسبك؟",
    "No appointment is available at that time. Please choose another time.",
    "ما في موعد متاح بهالساعة. اختار ساعة تانية، لو سمحت.",
    "Please choose one of the displayed option numbers.", "اختار رقم موعد واحد من القائمة المعروضة، لو سمحت.",
    "Please search for appointments first.", "خلينا نبحث عن مواعيد متاحة أولًا.",
    "Please complete the appointment details first.", "خلينا نكمّل تفاصيل الموعد أولًا.",
    "Could you clarify the appointment details?", "ممكن توضّح تفاصيل الموعد؟",
    "We need to verify the booking details first.", "لازم نتحقق من بيانات الحجز أولًا.",
    "Use Approve to confirm, or Reject to choose another appointment.",
    "للتأكيد اضغط زر الموافقة، أو الرفض إذا بدك موعد تاني.",
    "Please provide a valid phone number starting with 0 or +970/+972.",
    "أعطيني رقم تلفون صحيح يبدأ بصفر أو +970/+972.",
    "Please repeat or type the full number. Local mobile numbers need 10 digits.",
    "احكي أو اكتب الرقم كامل. رقم الجوال المحلي لازم يكون 10 خانات.",
    "The prefix is unsupported. Please repeat or type the full number.",
    "بداية الرقم غير مدعومة. احكي أو اكتب الرقم كامل.",
    "Please say digits one by one or type the number.", "احكي الأرقام رقم رقم أو اكتب الرقم.",
    "I couldn't verify that the appointments are still available. Please try again.",
    "ما قدرت أتأكد إذا المواعيد لسه متاحة. جرّب مرة تانية، لو سمحت.",
    "Which appointment or time should I search before?", "قبل أي موعد أو ساعة بدك أدور؟",
    "I couldn't check earlier appointments. Please try again.",
    "ما قدرت أتحقق من المواعيد الأبكر. جرّب مرة تانية، لو سمحت.",
    "There are no matching earlier appointments on that date.", "ما في مواعيد أبكر بتطابق طلبك بهاليوم.",
    "Booking details changed. Please review the appointment again.", "تفاصيل الموعد تغيّرت. راجعها مرة تانية، لو سمحت.",
    "This slot is no longer available. Please choose another appointment.", "هالموعد ما عاد متاح. خلينا نختار موعد تاني.",
    "The booking could not be confirmed. Please try again.", "ما قدرنا نؤكد الحجز. جرّب مرة تانية، لو سمحت.",
    "Appointment selected.", "تم اختيار الموعد.",
    "قصدك صباحًا ولا مساءً؟",
    "Please say the complete appointment time.", "ممكن تحكيلي الساعة كاملة؟",
    "That time and doctor are not together in the displayed options. Which option would you prefer?",
    "هالساعة مع هالطبيب مش ضمن الخيارات المعروضة. أي خيار بناسبك؟",
    "More than one time is available with this doctor. Which time would you prefer?",
    "في أكتر من وقت متاح عند هالطبيب. أي ساعة بتناسبك؟",
    "This appointment does not match your request. Please choose another.",
    "هالموعد ما بطابق طلبك. اختار موعد تاني، لو سمحت.",
    "I couldn't verify the new time. Please try again.",
    "ما قدرت أتحقق من الساعة الجديدة. جرّب مرة تانية، لو سمحت.",
    "I cannot check appointments right now. Please try again.",
    "مش قادر أتحقق من المواعيد هلأ. جرّب مرة تانية، لو سمحت.",
    "I cannot reach the clinic right now. Please try again.",
    "التواصل مع العيادة مش متاح هلأ. جرّب مرة تانية، لو سمحت.",
    "I cannot verify the clinic information right now. Please try again.",
    "تعذر التحقق من معلومات العيادة حاليًا. يرجى المحاولة مجددًا.",
    "I couldn't verify the requested provider for this service. Could you clarify the provider's name?",
    "لم أتمكن من التحقق من الطبيب الذي طلبته لهذه الخدمة. هل يمكنك توضيح اسم الطبيب؟",
    "Your appointment options are still available. Which option would you prefer?",
    "خيارات موعدك بعدها موجودة. أي خيار بناسبك؟",
    "How old is the patient?", "كم عمر المريض؟",
    "What is the guardian's full name?", "شو اسم ولي الأمر الكامل؟",
    "Did you mean morning or afternoon?",
    "Which day would you like the appointment?", "في أي يوم ترغب بحجز الموعد؟",
    "What is the patient's full name?", "شو اسم المريض الكامل؟",
    "What is your phone number?", "شو رقم تلفونك؟",
    "Which doctor would you prefer?", "عند أي طبيب بتفضّل؟",
    "Please repeat the exact hour and minute.", "ممكن تعيد الساعة والدقيقة بالضبط؟",
    "That date has passed. Which upcoming day works for you?", "هالتاريخ مرق. أي يوم جاي بناسبك؟",
    "What time would you prefer: morning or afternoon?", "أي ساعة بتناسبك، صباحًا ولا مساءً؟",
    "Please choose one of the available appointments.", "اختار موعد من الخيارات المتاحة، لو سمحت.",
    "No matching appointments are available. Would you like another time?",
    "ما في موعد متاح بطابق طلبك. بتحب وقت تاني؟",
}
AR_HOURS = "الواحدة الثانية الثالثة الرابعة الخامسة السادسة السابعة الثامنة التاسعة العاشرة".split() + ["الحادية عشرة", "الثانية عشرة"]
AR_UNITS = "صفر واحدة اثنتان ثلاث أربع خمس ست سبع ثمان تسع".split()
AR_TENS = "صفر عشرة عشرون ثلاثون أربعون خمسون".split()
EN_NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
EN_TENS = "zero ten twenty thirty forty fifty".split()

router = APIRouter()
tickets = OrderedDict()
active_tickets = set()
synthetic_exact_speech = ContextVar("synthetic_exact_speech", default=False)
PRIVATE_COMPANIONS = {
    "awaiting_phone_confirmation": {
        "ar": "وصلني رقم التلفون. تأكد إنه صحيح على الشاشة، واحكي نعم إذا بدك نكمل.",
        "en": "I received your phone number. Check it on the screen and say yes if it is correct.",
    },
    "awaiting_approval": {
        "ar": "جهزت تفاصيل حجزك للمراجعة على الشاشة. راجعها، وإذا كل شيء صحيح اضغط تأكيد الحجز.",
        "en": "Your appointment details are ready to review on the screen. If everything is correct, select Confirm booking.",
    },
}


def _arabic_minutes(minute):
    if minute in (0, 1, 2, 15, 30):
        return {0: "", 1: " ودقيقة", 2: " ودقيقتين", 15: " والربع",
                30: " والنصف"}[minute]
    if minute < 11:
        return " و" + (AR_UNITS[minute] if minute < 10 else "عشر") + " دقائق"
    if minute < 20:
        teens = ["إحدى عشرة", "اثنتا عشرة", "ثلاث عشرة", "أربع عشرة",
                 "خمس عشرة", "ست عشرة", "سبع عشرة", "ثماني عشرة", "تسع عشرة"]
        return " و" + teens[minute - 11] + " دقيقة"
    tens, units = divmod(minute, 10)
    return " و" + (AR_UNITS[units] + " و" if units else "") + AR_TENS[tens] + " دقيقة"


def format_speech(text, language):
    text = re.sub(r"(?m)^\s*[0-9]+\. (?=[0-9]{1,2}:[0-9]{2})", "", text)
    if language == "ar":
        for provider in PROVIDERS.values():
            name = provider["name_ar"]
            if name.startswith("د. "):
                title = "الدكتورة" if provider["id"] in ("dr_khoury", "dr_mansour") else "الدكتور"
                text = text.replace(name, title + " " + name[3:])
        def arabic_clock(match):
            hour, minute = map(int, match.group(1, 2))
            spoken_hour = AR_HOURS[hour - 1]
            return "الساعة " + spoken_hour + _arabic_minutes(minute) + " " + match[3]
        return re.sub(r"(?:الساعة )?(1[0-2]|[1-9]):([0-5][0-9]) (صباحًا|مساءً)", arabic_clock, text)

    def english_clock(match):
        hour, minute = map(int, match.group(1, 2))
        if minute < 20:
            minutes = EN_NUMBERS[minute]
        else:
            tens, units = divmod(minute, 10)
            minutes = EN_TENS[tens] + (" " + EN_NUMBERS[units] if units else "")
        return EN_NUMBERS[hour] + ((" oh " if minute < 10 else " ") + minutes if minute else "") + " " + match[3][0].lower() + ".m."

    text = re.sub(r"(1[0-2]|[1-9]):([0-5][0-9]) (AM|PM)", english_clock, text)
    return text.replace("Dr. ", "Doctor ")


def public_speech(response, state):
    """Accept exact public templates. Free-form, identifying and review text stays local."""
    language = response.get("language")
    if language not in ("ar", "en") or response.get("session_reset") or response.get("validation_error") or response.get("speech_error"):
        return None
    reply = response.get("reply")
    if not isinstance(reply, str):
        return None
    if "appointment_history" in response:
        if response["appointment_history"]:
            return say(response, "Your bookings from this conversation are on the screen.",
                       "مواعيدك من هالمحادثة موجودة على الشاشة.")
        return say(response, "There are no bookings in this conversation. Contact the clinic to verify your identity.",
                   "ما في حجوزات بهالمحادثة. تواصل مع العيادة للتحقق من هويتك.")
    stage = response.get("state")
    if reply == state.get("inquiry_reply"):
        return format_speech(reply, language)
    if stage in PRIVATE_COMPANIONS:
        if reply in PUBLIC_PROMPTS:
            return format_speech(reply, language)
        if stage == "awaiting_phone_confirmation" and reply.startswith(("I heard your phone number as ", "سمعت رقم تلفونك ")):
            return PRIVATE_COMPANIONS[stage][language]
        if stage == "awaiting_approval" and reply.startswith(("Please review and approve: ", "راجع التفاصيل ووافق لتأكيد الحجز: ")):
            return PRIVATE_COMPANIONS[stage][language]
        if stage == "awaiting_approval" and reply.startswith(("Review and approve: ", "راجع ووافق: ")) and (response.get("appointment_review") or {}).get("type") == "cancel_booking":
            return say(response, "Review the cancellation details on the screen. Confirm only if they are correct.",
                       "راجع تفاصيل الإلغاء على الشاشة، وأكد الإلغاء إذا كانت صحيحة.")
        return None
    if response.get("appointment_review") or stage in ("booking_confirmed", "booking_cancelled"):
        return None
    if reply in PUBLIC_PROMPTS:
        return format_speech(reply, language)
    if stage == "awaiting_details":
        catalog = [SERVICES[row["id"]] for row in state.get("service_catalog", [])
                   if isinstance(row, dict) and row.get("id") in SERVICES]
        safe_state = {**state, "service_catalog": catalog, "language": language}
        if reply != service_question(safe_state):
            return None
    elif stage in ("slots_proposed", "awaiting_slot_selection"):
        slots = state.get("proposed_slots") or []
        safe_slots = []
        for slot in slots[:8]:
            provider = PROVIDERS.get(slot.get("provider_id"))
            day, start = slot.get("date", ""), slot.get("start", "")
            if not provider or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start):
                return None
            safe_slots.append({"date": day, "start": start, "provider_name": provider["name"],
                               "provider_name_ar": provider["name_ar"]})
        if not safe_slots:
            return None
        options = format_slot_options(safe_slots, {"language": language})
        prefixes = [
            "",
            say(response, "That time is outside clinic opening hours. These times are available:\n",
                "هالساعة خارج دوام العيادة. هاي مواعيد متاحة:\n"),
            say(response, "The clinic is closed on your requested day. These later dates are available:\n",
                "العيادة مسكّرة باليوم المطلوب. هاي خيارات بأقرب يوم لاحق:\n"),
            say(response, "Your preferred doctor is unavailable. These alternatives are available:\n",
                "ما في موعد عند طبيبك المفضّل. هاي خيارات عند أطباء تانيين:\n"),
        ]
        requested = state.get("requested_time")
        if isinstance(requested, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", requested):
            label = display_time(requested, language)
            prefixes.extend([
                say(response, f"{label} is unavailable. These nearby times are available:\n",
                    f"الساعة {label} مش متاحة. هاي مواعيد قريبة متاحة:\n"),
                say(response, f"{label} is unavailable. These other times are available:\n",
                    f"الساعة {label} مش متاحة. هاي مواعيد تانية متاحة:\n"),
            ])
        if reply not in [prefix + options for prefix in prefixes]:
            suffix = reply.removeprefix(options + "\n")
            if stage != "awaiting_slot_selection":
                return None
            if suffix != reply and suffix in PUBLIC_PROMPTS:
                return format_speech(reply, language)
            candidates = set(state.get("selection_candidates") or [])
            doctor_key = "name_ar" if language == "ar" else "name"
            doctors = list(dict.fromkeys(
                PROVIDERS[slot["provider_id"]][doctor_key]
                for slot in slots[:8] if slot.get("slot_id") in candidates
            ))
            question = say(response, "Which doctor would you prefer: ", "عند أي طبيب بتفضّل: ")
            if len(doctors) < 2 or suffix != question + "، ".join(doctors) + "؟":
                return None
    else:
        return None
    return format_speech(reply, language)


def attach_speech(response, state):
    if response.get("language") not in ("ar", "en"):
        response = {**response, "language": "ar" if re.search(r"[\u0600-\u06ff]", response.get("reply") or "") else "en"}
    changed = response.get("appointment_selection_changed")
    speech_response = {**response, "reply": response["reply"].partition("\n")[2]} if changed else response
    text = public_speech(speech_response, state)
    if changed and not response.get("session_reset"):
        notice = say(response, "The selected appointment changed. The new time is on the screen.",
                     "تغيّر الموعد المختار. الوقت الجديد موجود على الشاشة.")
        text = f"{notice} {text}" if text else notice
    if not text:
        stage = response.get("state")
        result = response.get("result") or {}
        error = response.get("speech_error")
        if error == "phone_transcript_rejected":
            text = say(response, "Please say the phone digits one by one, or type the full number.",
                       "احكي أرقام التلفون رقم رقم، أو اكتب الرقم كامل.")
        elif error == "voice":
            text = say(response, "Please try recording again, or type your message.",
                       "جرّب تسجّل مرة تانية، أو اكتب رسالتك.")
        elif error or response.get("validation_error"):
            text = say(response, "I couldn't process that request. Please check the message on the screen and try again.",
                       "ما قدرت أعالج الطلب. راجع الرسالة على الشاشة وجرّب مرة تانية.")
        elif response.get("session_reset"):
            text = say(response, "The conversation expired. Please review the message on the screen.",
                       "انتهت المحادثة السابقة. راجع الرسالة على الشاشة، لو سمحت.")
        elif result.get("ok") is True and result.get("booking_id") and stage == "booking_confirmed":
            text = say(response, "Your appointment is confirmed. Your booking reference is on the screen.",
                       "تم تأكيد موعدك. رقم الحجز موجود على الشاشة.")
        elif result.get("ok") is True and result.get("booking_id") and stage == "booking_cancelled":
            text = say(response, "Your appointment was cancelled. The details are on the screen.",
                       "تم إلغاء موعدك. التفاصيل موجودة على الشاشة.")
        else:
            text = say(response, "Please check my message on the screen for the next step.",
                       "راجع رسالتي على الشاشة لتعرف الخطوة الجاية، لو سمحت.")
    written = response.get("reply")
    success_claim = isinstance(written, str) and written.startswith((
        "Your appointment is confirmed.", "تم تأكيد موعدك.", "Appointment cancelled.", "تم إلغاء الموعد.",
    ))
    result = response.get("result") or {}
    if synthetic_exact_speech.get() and isinstance(written, str) and written.strip() and (
        not success_claim or (result.get("ok") is True and result.get("booking_id"))
    ):
        text = written
    now = monotonic()
    while tickets and (next(iter(tickets.values()))[0] <= now or len(tickets) >= 256):
        tickets.popitem(last=False)
    token = uuid4().hex
    tickets[token] = (now + 120, text, response["language"])
    return {**response, "speech_token": token, "speech_route": "gemini"}


@router.post("/api/speech/{token}", include_in_schema=False)
async def speech(token: str):
    ticket = tickets.get(token)
    key = os.getenv("OPENROUTER_API_KEY")
    if not ticket or ticket[0] <= monotonic():
        raise HTTPException(404, "Speech unavailable", headers={"X-Speech-Outcome": "ticket_unavailable"})
    if not key or token in active_tickets:
        raise HTTPException(503, "Speech unavailable", headers={"X-Speech-Outcome": "provider_failure"})
    _, text, language = ticket
    instructions = ("Warm, natural Palestinian Arabic receptionist. Use clear conversational pacing."
                    if language == "ar" else "Warm, natural conversational English receptionist.")
    instructions += " Read the text as written, pronouncing names, dates and times clearly."
    base = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    client = httpx.AsyncClient(timeout=httpx.Timeout(12, connect=4), follow_redirects=False)
    upstream = None
    active_tickets.add(token)
    started = monotonic()
    try:
        async with asyncio.timeout(15):
            request = client.build_request("POST", base + "/audio/speech",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL, "voice": VOICE, "input": text,
                      "instructions": instructions, "response_format": "pcm"})
            upstream = await client.send(request, stream=True)
            upstream.raise_for_status()
            if upstream.headers.get("content-type", "").split(";")[0].strip() != "audio/pcm":
                raise ValueError("Unexpected audio content type")
            chunks = upstream.aiter_bytes()
            first = await anext(chunks)
            first_byte_ms = (monotonic() - started) * 1000
    except BaseException as error:
        active_tickets.discard(token)
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        if isinstance(error, (httpx.HTTPError, TimeoutError, ValueError, StopAsyncIteration)):
            category = "provider_payload" if isinstance(error, (ValueError, StopAsyncIteration)) else "provider_failure"
            logging.getLogger(__name__).info("speech_outcome=%s", category)
            raise HTTPException(503, "Speech unavailable", headers={"X-Speech-Outcome": category}) from None
        raise

    async def stream():
        size = len(first)
        try:
            async with asyncio.timeout(20):
                if size > MAX_AUDIO_BYTES:
                    raise ValueError("Speech limit exceeded")
                yield first
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise ValueError("Speech limit exceeded")
                    yield chunk
        except (httpx.HTTPError, TimeoutError, ValueError):
            logging.getLogger(__name__).info("speech_outcome=stream_interrupted")
            raise RuntimeError("Speech stream interrupted") from None
        finally:
            active_tickets.discard(token)
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(stream(), media_type="audio/pcm",
                             headers={"Cache-Control": "no-store", "X-Audio-Sample-Rate": str(PCM_RATE),
                                      "Server-Timing": f"tts_first_byte;dur={first_byte_ms:.1f}"})
