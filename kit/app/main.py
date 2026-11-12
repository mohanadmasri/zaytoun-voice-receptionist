# واجهة FastAPI للمساعد: تربط HTTP بمنطق المحادثة والموافقة الموجود.
# المدخل JSON مطابق للعقد؛ المخرج ChatResponse أو خطأ تحقق/تعذر خدمة.
# يصل LLM عبر handle_message، وتصل أدوات MCP عبر Gateway فقط.
# لا ينفذ مسار chat كتابة؛ الكتابة محصورة بمنطق handle_approval الموثوق.
# الحالة والأقفال في RAM؛ شغّل worker واحدًا حتى إضافة تخزين مشترك.
# الصوت والنص يشتركان في process_message؛ الكتابة تبقى في مسار الموافقة فقط.
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from uuid import uuid4
from collections import defaultdict
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, File, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from actions.approval import handle_approval
from agent.runner import handle_message
from core.contracts import ClinicGateway as GatewayProtocol
from integrations.mcp_gateway import ClinicGateway
from integrations.voice_adapter import VoiceAdapter, VoiceError, MAX_AUDIO_BYTES, EMPTY_AUDIO
from core.session_store import session_store
from agent.presentation import say
from integrations.clinic_speech import attach_speech, router as speech_router, synthetic_exact_speech
from agent.patient_identity import valid_patient_name


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# يتحقق Pydantic من طلب المحادثة قبل وصوله إلى Graph.
# user بيانات اختيارية حسب العقد، ولا تُستخدم لتجاوز استخراج بيانات المريض.
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmptyText
    message: NonEmptyText
    user: dict[str, Any] | None = None
    continuation: StrictBool = False


# يرفض payload من المتصفح؛ مصدر بيانات الحجز هو SessionStore فقط.
# StrictBool يمنع تفسير نص أو رقم على أنه موافقة صريحة.
class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmptyText
    action_id: NonEmptyText
    approved: StrictBool


# تغلف البوابة لكل طلب وتسجل كل القراءات والكتابات بالشكل المطلوب في العقد.
# لا تحفظ معاملات الأدوات أو بيانات المرضى في السجل؛ تحفظ الاسم والنجاح والمدة فقط.
class TracedGateway:
    def __init__(self, gateway: GatewayProtocol):
        self.gateway = gateway
        self.tool_calls = []
        self.read_cache = {}

    # تنفذ استدعاء واحدًا وتعيد نتيجته كما هي؛ تسجل الفشل حتى عند رفع استثناء.
    async def _call(self, method, tool_name: str, args: dict) -> dict:
        started = perf_counter()
        success = False
        try:
            result = await method(tool_name, args)
            success = isinstance(result, dict) and result.get("ok") is True
            return result
        finally:
            self.tool_calls.append({"tool": tool_name, "ok": success,
                                    "ms": round((perf_counter() - started) * 1000, 2)})

    # تستقبل اسم أداة قراءة ومعاملاتها وتعيد نتيجة MCP دون تغيير صلاحيات البوابة.
    async def call_read(self, tool_name: str, args: dict) -> dict:
        key = (tool_name, json.dumps(args, sort_keys=True))
        if tool_name in ("clinic_info", "list_services") and key in self.read_cache:
            from copy import deepcopy
            return deepcopy(self.read_cache[key])
        result = await self._call(self.gateway.call_read, tool_name, args)
        if tool_name in ("clinic_info", "list_services") and isinstance(result, dict) and result.get("ok") is True:
            from copy import deepcopy
            self.read_cache[key] = deepcopy(result)
        return result

    # تمرر كتابة من Approval Handler فقط؛ لا تمنح LLM صلاحية كتابة جديدة.
    async def call_write(self, tool_name: str, args: dict) -> dict:
        return await self._call(self.gateway.call_write, tool_name, args)


app = FastAPI(title="Zaytoun Voice Receptionist")
app.include_router(speech_router)
web_root = Path(__file__).resolve().parents[1] / "ui" / "web"
app.mount("/clinic-assets", StaticFiles(directory=web_root), name="clinic-assets")


@app.get("/", include_in_schema=False)
async def clinic_web():
    return FileResponse(web_root / "index.html", headers={"Cache-Control": "no-cache"})


app.state.session_locks = defaultdict(asyncio.Lock)
app.state.voice_reviews = {}
app.state.voice_generations = {}
app.state.last_audio = {}

CONVERSATION_PATHS = {"/api/chat", "/api/approve", "/api/voice", "/api/voice/confirm"}


@app.middleware("http")
async def scope_synthetic_speech(request: Request, call_next):
    enabled = (
        os.getenv("ZAYTOUN_DEMO_EXACT_SPEECH") == "synthetic-only"
        and request.url.path in CONVERSATION_PATHS
        and request.client is not None and request.client.host in ("127.0.0.1", "::1")
        and request.url.hostname in ("localhost", "127.0.0.1", "::1")
        and not any(header in request.headers for header in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
    )
    token = synthetic_exact_speech.set(enabled)
    try:
        return await call_next(request)
    finally:
        synthetic_exact_speech.reset(token)


def speak_api_error(request: Request, response):
    if request.url.path not in CONVERSATION_PATHS or not isinstance(response, JSONResponse):
        return response
    data = json.loads(response.body)
    data.update(language=request.headers.get("x-clinic-language"), speech_error="request")
    data["reply"] = say(data, "I couldn't process that request. Please try again.",
                        "ما قدرت أعالج الطلب. جرّب مرة تانية، لو سمحت.")
    return JSONResponse(attach_speech(data, {}), status_code=response.status_code,
                        headers={key: value for key, value in response.headers.items() if key != "content-length"})


@app.exception_handler(HTTPException)
async def conversation_http_error(request: Request, error: HTTPException):
    return speak_api_error(request, await http_exception_handler(request, error))


@app.exception_handler(RequestValidationError)
async def conversation_validation_error(request: Request, error: RequestValidationError):
    return speak_api_error(request, await request_validation_exception_handler(request, error))


@app.middleware("http")
async def log_voice_outcome(request: Request, call_next):
    if request.method != "POST" or request.url.path != "/api/voice":
        return await call_next(request)

    started = perf_counter()
    outcome = {
        "event": "voice_request_outcome", "request_id": uuid4().hex,
        "stage": "request_validation", "category": "request_validation",
        "audio_format": None, "audio_bytes": None, "audio_duration_ms": None,
        "stt_reached": False, "stt_outcome": "not_reached", "transcript_rejected": False,
    }
    request.state.voice_outcome = outcome
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Voice-Request-ID"] = outcome["request_id"]
        if status < 400:
            outcome["category"] = "success"
        elif status >= 500 and outcome["category"] == "request_validation":
            outcome["category"] = "unexpected_backend_error"
        return response
    except Exception:
        outcome["category"] = "unexpected_backend_error"
        raise
    finally:
        outcome.update(http_status=status, elapsed_ms=round((perf_counter() - started) * 1000, 2))
        # Serialize only server-generated categories and numeric/boolean measurements.
        logging.getLogger("uvicorn.error").info("%s", json.dumps(outcome, separators=(",", ":")))


# بصمة الحالة تمنع تطبيق تفريغ قديم بعد نص جديد أو تغيير موافقة؛ لا نسجل محتواها أو بيانات المريض.
def state_fingerprint(session_id: str) -> str:
    state = session_store.get_or_create(session_id)
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def appointment_review(state: dict) -> dict | None:
    pending = state.get("pending_action")
    if not pending:
        return None
    payload = pending["payload"]
    if pending["type"] == "cancel_booking":
        slot = state.get("cancellation_record") or {}
        patient = slot.get("patient") or {}
    else:
        slot = next((item for item in state.get("proposed_slots", [])
                     if item.get("slot_id") == payload.get("slot_id")), {})
        patient = {"name": payload.get("patient_name"), "phone": payload.get("patient_phone")}
    service = next((item for item in state.get("service_catalog", [])
                    if item["id"] == (slot.get("service_id") or payload.get("service_id"))), {})
    provider = next((item for item in state.get("provider_catalog", [])
                     if item["id"] == slot.get("provider_id")), {})
    phone = patient.get("phone")
    return {
        "action_id": pending["action_id"], "type": pending["type"],
        "service_name": slot.get("service_name") or service.get("name_en"),
        "service_name_ar": slot.get("service_name_ar") or service.get("name_ar"),
        "provider_name": slot.get("provider_name") or provider.get("name"),
        "provider_name_ar": slot.get("provider_name_ar") or provider.get("name_ar"),
        "date": slot.get("date"), "start": slot.get("start"),
        "patient_name": patient.get("name"), "masked_phone": f"•••• {phone[-4:]}" if phone else None,
    }


# تأكيد النص لا يمثل موافقة حجز؛ لا يقبل العميل نصًا بديلًا أو payload حجز.
class VoiceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmptyText
    transcript_token: NonEmptyText


class SessionRequest(BaseModel):
    session_id: NonEmptyText


# بدء تسجيل جديد يبطل رد STT أقدم فقط، دون لمس الموعد أو هوية المريض.
@app.post("/api/voice/discard")
async def discard_voice(body: SessionRequest):
    async with app.state.session_locks[body.session_id]:
        app.state.voice_generations.pop(body.session_id, None)
        app.state.voice_reviews.pop(body.session_id, None)
    return {"status": "ok"}


# تنشئ بوابة حقيقية لكل طلب؛ Depends تسمح باستبدالها ببوابة وهمية في الاختبار.
def get_gateway():
    return ClinicGateway()


# توفر محول الصوت الموجود لكل طلب؛ يمكن استبداله بمحاكاة لا تستخدم الشبكة في الاختبار.
def get_voice_adapter():
    return VoiceAdapter()


# تعيد حالة خدمة HTTP فقط؛ لا تستدعي LLM أو العيادة ولا تؤكد جاهزيتهما.
@app.get("/health")
async def health():
    return {"status": "ok"}


# تستقبل معرف الجلسة والنص والبوابة، وتعيد استجابة Graph مع سجل الجولة كاملًا.
# هذه نقطة المعالجة المشتركة للنص والصوت؛ لا نرسل HTTP من الخادم إلى نفسه.
# القفل يشمل قراءة الحالة ومعالجتها وحفظها لمنع ضياع تحديثات الجلسة المتزامنة.
async def process_message(session_id: str, message: str, gateway, continuation: bool = False, input_language: str | None = None, expected_state: str | None = None, expected_generation: str | None = None):
    started = perf_counter()
    traced = TracedGateway(gateway)
    async with app.state.session_locks[session_id]:
        if expected_state and expected_state != state_fingerprint(session_id):
            raise HTTPException(409, "The conversation changed. Please record again / المحادثة تغيّرت، أعد التسجيل.")
        if expected_generation and app.state.voice_generations.get(session_id) != expected_generation:
            raise HTTPException(409, "A newer recording replaced this one / وصل تسجيل أحدث، أعد المراجعة.")
        app.state.voice_reviews.pop(session_id, None)
        app.state.voice_generations.pop(session_id, None)
        session_reset = continuation and not session_store.contains(session_id)
        try:
            response = (await handle_message(session_id, message, traced, input_language)
                        if input_language else await handle_message(session_id, message, traced))
        except Exception as error:
            logging.getLogger(__name__).warning("chat_processing_failed type=%s elapsed_ms=%.2f", type(error).__name__, (perf_counter() - started) * 1000)
            # لا نعيد نص الاستثناء الذي قد يحتوي معلومات داخلية أو حساسة.
            language = input_language or session_store.get_or_create(session_id).get("language")
            raise HTTPException(503, "تعذر معالجة الرسالة. جرّب مرة تانية." if language == "ar" else "Unable to process the message. Please try again.") from None
        state = session_store.get_or_create(session_id)
        review = appointment_review(state)
    if session_reset:
        response["reply"] = say(response, "The previous session is no longer available; let's review the appointment again. ",
                                "الجلسة السابقة مش موجودة؛ خلينا نراجع الموعد من جديد. ") + response["reply"]
    formatting = perf_counter()
    result = {**response, "tool_calls": traced.tool_calls, "session_reset": session_reset,
              "appointment_review": review, "session_appointments": state.get("session_appointments", [])}
    result["timings"] = {**response.get("timings", {}), "processing_ms": round((formatting - started) * 1000, 2), "formatting_ms": round((perf_counter() - formatting) * 1000, 3)}
    ticket_started = perf_counter()
    response = attach_speech(result, state)
    response["timings"]["speech_ticket_ms"] = round((perf_counter() - ticket_started) * 1000, 3)
    return response


# تستقبل JSON المفحوص بـPydantic وتستخدم نفس المعالجة التي يستدعيها مسار الصوت.
@app.post("/api/chat", response_model=None)
async def chat(body: ChatRequest, gateway=Depends(get_gateway)):
    return await process_message(body.session_id, body.message, gateway, body.continuation)


# تعيد خطأ صوتيًّا واضحًا مع حالة الجلسة وموافقتها الحالية دون تغييرهما.
# تبقى أزرار الموافقة ظاهرة إذا فشل التسجيل بعد تجهيز طلب سابق.
async def voice_error_response(session_id: str, error: VoiceError, transcript: str = "", language_hint: str | None = None):
    async with app.state.session_locks[session_id]:
        state = session_store.get_or_create(session_id)
    if language_hint in ("ar", "en"):
        state["language"] = language_hint
    response = {"session_id": session_id, "reply": str(error), "tool_calls": [],
                "state": state.get("stage"), "language": state.get("language", "en"), "transcript": transcript, **EMPTY_AUDIO}
    if state.get("language") == "ar":
        response["reply"] = {
            413: "التسجيل طويل. جرّب تسجيل أقصر، لو سمحت.",
            415: "استخدم تسجيل WAV أو MP3، أو اكتب رسالتك.",
            422: "التفريغ مش واضح أو ما بطابق لغة التسجيل. ممكن تعيد التسجيل باللغة المختارة أو تكتب رسالتك؟",
            504: "الرد أخذ وقت طويل. جرّب مرة تانية أو اكتب رسالتك.",
        }.get(error.status_code, "خدمة الصوت مش متاحة هلأ. جرّب تكتب رسالتك.")
    if error.category in ("stt_provider_error", "stt_provider_timeout", "stt_deadline", "stt_exception"):
        response["category"] = error.category
        response["reply"] = say(response,
            "Speech recognition is temporarily unavailable. Please record again or type your message.",
            "تعذّر التعرّف على الصوت مؤقتًا. ممكن إعادة التسجيل أو كتابة الرسالة.")
    if error.category == "phone_transcript_rejected":
        response["category"] = error.category
        response["reply"] = say(response, str(error),
                                "ما قدرت أميّز رقم التلفون. جرّب تحكي الأرقام رقم رقم أو اكتب الرقم.")
    if state.get("pending_action") is not None:
        response["pending_action"] = state["pending_action"]
    response["speech_error"] = "phone_transcript_rejected" if error.category == "phone_transcript_rejected" else "voice"
    return JSONResponse(status_code=error.status_code, content=attach_speech(response, {}))


# تستقبل multipart: التسجيل ومعرف الجلسة، وتعيد ChatResponse مع transcript وحقول الصوت.
# تفحص الحجم، وتفرغ التسجيل، ثم تستخدم نفس Graph والقفل والحماية مثل /api/chat.
# لا نحول كلمة yes أو نعم إلى موافقة؛ /api/approve وحدها تنفذ الكتابة.
@app.post("/api/voice", response_model=None)
async def voice(
    request: Request,
    audio: UploadFile = File(...), session_id: str = Form(...),
    language_hint: Literal["ar", "en"] | None = Form(None), continuation: bool = Form(False),
    preview_only: bool = Form(False), synthetic_phone_probe: str | None = Form(None),
    gateway=Depends(get_gateway), adapter=Depends(get_voice_adapter),
):
    outcome = request.state.voice_outcome
    outcome["audio_bytes"] = audio.size
    session_id = session_id.strip()
    if not session_id:
        outcome["category"] = "empty_session"
        await audio.close()
        raise HTTPException(422, "session_id must not be empty.")
    transcript = ""
    recognized = []
    generation = uuid4().hex
    outcome["stage"] = "session_setup"
    async with app.state.session_locks[session_id]:
        session_missing = continuation and not session_store.contains(session_id)
        app.state.voice_generations[session_id] = generation
        app.state.voice_reviews.pop(session_id, None)
        snapshot = state_fingerprint(session_id)
        original_state = session_store.get_or_create(session_id)
    try:
        # نقرأ حدًا أقصى معلومًا بدل تحميل ملف ضخم كاملًا في الذاكرة.
        outcome["stage"] = "audio_upload"
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        outcome["audio_bytes"] = audio.size if audio.size is not None else len(data)
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceError(413, "The recording is too large. Please use a shorter recording (up to 10 MB).", "audio_too_large")
        digest = hashlib.sha256(data).hexdigest()
        if app.state.last_audio.get(session_id) == digest:
            raise VoiceError(409, "This recording was already sent. Please make a new recording / أعد التسجيل؛ هذا الملف أُرسل سابقًا.", "duplicate_audio")
        phone_mode = bool(not original_state.get("time_change_pending") and original_state.get("selected_slot_id") and valid_patient_name(original_state.get("patient_name")) and
                          (not original_state.get("patient_phone") or original_state.get("confirmed_phone") != original_state.get("patient_phone")))
        probe_key = os.getenv("ZAYTOUN_SYNTHETIC_PHONE_PROBE")
        probe = bool(phone_mode and synthetic_phone_probe and synthetic_phone_probe.isascii()
                     and probe_key and probe_key.isascii() and hmac.compare_digest(synthetic_phone_probe, probe_key)
                     and request.client and request.client.host in ("127.0.0.1", "::1")
                     and request.url.hostname in ("localhost", "127.0.0.1", "::1"))
        if probe:
            os.environ.pop("ZAYTOUN_SYNTHETIC_PHONE_PROBE", None)
        outcome["stage"] = "transcription"
        try:
            if probe:
                transcript = await adapter.transcribe(data, language_hint, phone_mode=True, inspect_transcript=recognized.append)
            else:
                transcript = await adapter.transcribe(data, language_hint, phone_mode=True) if phone_mode else await adapter.transcribe(data, language_hint)
        finally:
            outcome.update(getattr(adapter, "diagnostics", {}))
        if probe:
            return JSONResponse({"synthetic_phone_transcript": transcript, "validation": "accepted"},
                                headers={"Cache-Control": "no-store"})
        outcome["stage"] = "preview"
        async with app.state.session_locks[session_id]:
            if app.state.voice_generations.get(session_id) != generation or state_fingerprint(session_id) != snapshot:
                raise VoiceError(409, "A newer recording or message replaced this one / وصل تسجيل أو رسالة أحدث، أعد التسجيل.", "stale_preview")
            token = uuid4().hex
            spoken_language = language_hint or ("ar" if any('\u0600' <= c <= '\u06ff' for c in transcript) else "en")
            app.state.voice_reviews[session_id] = {"token": token, "text": transcript, "language": spoken_language,
                "snapshot": snapshot, "generation": generation, "created": perf_counter(), "continuation": continuation, "session_missing": session_missing}
            app.state.last_audio[session_id] = digest
        if not preview_only:
            outcome["stage"] = "turn_processing"
            result = await confirm_voice(VoiceReviewRequest(session_id=session_id, transcript_token=token), gateway)
            if isinstance(result, dict):
                result["timings"] = {**getattr(adapter, "timings", {}), **result.get("timings", {})}
            else:
                outcome["category"] = {409: "stale_preview", 503: "backend_error", 504: "turn_timeout"}.get(
                    result.status_code, "turn_rejected",
                )
            return result
        language = spoken_language
        return attach_speech({"session_id": session_id, "transcript": transcript, "transcript_token": token,
                "needs_transcript_confirmation": True, "state": original_state.get("stage"), "language": language,
                "session_reset": session_missing,
                "reply": "راجع النص اللي سمعته. إذا صحيح اضغط «استخدم النص»، أو أعد التسجيل." if language == "ar" else "Review the transcript. If correct, select Use transcript; otherwise record again.",
                "pending_action": original_state.get("pending_action"), "tool_calls": [],
                "timings": getattr(adapter, "timings", {}), **EMPTY_AUDIO}, {})
    except VoiceError as error:
        outcome["category"] = error.category
        if error.phone_issue:
            outcome["phone_issue"] = error.phone_issue
        outcome["transcript_rejected"] = outcome["stage"] == "transcript_validation"
        if recognized:
            return JSONResponse({"synthetic_phone_transcript": recognized[0], "validation": error.category,
                                 "phone_issue": error.phone_issue}, headers={"Cache-Control": "no-store"})
        return await voice_error_response(session_id, error, transcript, language_hint)
    except TimeoutError:
        outcome["category"] = "voice_deadline"
        return await voice_error_response(session_id, VoiceError(504, "The voice request took too long. Please try again."), transcript, language_hint)
    except HTTPException as error:
        outcome["category"] = "stale_preview" if error.status_code == 409 else "http_error"
        return await voice_error_response(session_id, VoiceError(error.status_code, str(error.detail)), transcript, language_hint)
    except Exception:
        outcome["category"] = "unexpected_backend_error"
        return await voice_error_response(session_id, VoiceError(503, "Unable to process the recording. Please try again or type your message."), transcript, language_hint)
    finally:
        await audio.close()


# لا تطبق نتيجة STT على الحالة إلا بعد مراجعة المريض للنص نفسه، وبنفس نسخة الجلسة.
@app.post("/api/voice/confirm", response_model=None)
async def confirm_voice(body: VoiceReviewRequest, gateway=Depends(get_gateway)):
    async with app.state.session_locks[body.session_id]:
        review = app.state.voice_reviews.get(body.session_id)
        if not review or review["token"] != body.transcript_token or perf_counter() - review["created"] > 300:
            raise HTTPException(409, "Transcript expired or replaced / النص قديم أو تم استبداله، أعد التسجيل.")
        app.state.voice_reviews.pop(body.session_id, None)
    try:
        response = await asyncio.wait_for(process_message(body.session_id, review["text"], gateway,
            review["continuation"], review["language"], review["snapshot"], review["generation"]), timeout=60)
    except TimeoutError:
        return await voice_error_response(body.session_id, VoiceError(504, "Please try again or type your message."), review["text"], review["language"])
    except HTTPException as error:
        return await voice_error_response(body.session_id, VoiceError(error.status_code, str(error.detail)), review["text"], review["language"])
    if review["session_missing"]:
        response["session_reset"] = True
        response["reply"] = say(response, "The previous session is unavailable. ", "الجلسة السابقة مش موجودة. ") + response["reply"]
        response = attach_speech(response, {})
    return {**response, "transcript": review["text"], **EMPTY_AUDIO}


# تستقبل القرار فقط وتعيد نتيجة Approval Handler؛ لا تنشئ payload من HTTP.
# تشترك مع chat في قفل الجلسة كي لا تتغير تفاصيل الحجز أثناء الموافقة.
@app.post("/api/approve", response_model=None)
async def approve(body: ApprovalRequest, gateway=Depends(get_gateway)):
    traced = TracedGateway(gateway)
    async with app.state.session_locks[body.session_id]:
        app.state.voice_reviews.pop(body.session_id, None)
        app.state.voice_generations.pop(body.session_id, None)
        try:
            before = appointment_review(session_store.get_or_create(body.session_id))
            response = await handle_approval(body.session_id, body.action_id, body.approved, traced)
        except Exception:
            raise HTTPException(503, "Unable to process approval. Please try again.") from None
        state = session_store.get_or_create(body.session_id)
        review = appointment_review(state)
        records = state.get("session_appointments", [])
        result = response.get("result") or {}
        appointment = None
        if result.get("ok") and result.get("booking_id") and result.get("status") in ("confirmed", "cancelled"):
            fields = ("booking_id", "status", "date", "start", "service_name",
                      "service_name_ar", "provider_name", "provider_name_ar")
            appointment = {key: result.get(key) or (before or {}).get(key) for key in fields}
            existing = next((row for row in records if row["booking_id"] == appointment["booking_id"]), None)
            if existing is None and appointment["status"] == "confirmed":
                records.append(appointment)
            elif existing is not None and appointment["status"] == "cancelled":
                existing.update({key: value for key, value in appointment.items() if value is not None})
            state["session_appointments"] = records
            session_store.save(state)
    return attach_speech({**response, "tool_calls": traced.tool_calls, "appointment_review": review,
                          "appointment": appointment, "session_appointments": records}, {})
