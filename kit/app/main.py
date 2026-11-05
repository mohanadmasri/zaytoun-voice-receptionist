# واجهة FastAPI للمساعد: تربط HTTP بمنطق المحادثة والموافقة الموجود.
# المدخل JSON مطابق للعقد؛ المخرج ChatResponse أو خطأ تحقق/تعذر خدمة.
# يصل LLM عبر handle_message، وتصل أدوات MCP عبر Gateway فقط.
# لا ينفذ مسار chat كتابة؛ الكتابة محصورة بمنطق handle_approval الموثوق.
# الحالة والأقفال في RAM؛ شغّل worker واحدًا حتى إضافة تخزين مشترك.
# الصوت والنص يشتركان في process_message؛ الكتابة تبقى في مسار الموافقة فقط.
import asyncio
import hashlib
import json
import logging
from uuid import uuid4
from collections import defaultdict
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from actions.approval import handle_approval
from agent.runner import handle_message
from core.contracts import ClinicGateway as GatewayProtocol
from integrations.mcp_gateway import ClinicGateway
from integrations.voice_adapter import VoiceAdapter, VoiceError, MAX_AUDIO_BYTES, EMPTY_AUDIO
from core.session_store import session_store
from agent.presentation import say
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
app.state.session_locks = defaultdict(asyncio.Lock)
app.state.voice_reviews = {}
app.state.voice_generations = {}
app.state.last_audio = {}


# بصمة الحالة تمنع تطبيق تفريغ قديم بعد نص جديد أو تغيير موافقة؛ لا نسجل محتواها أو بيانات المريض.
def state_fingerprint(session_id: str) -> str:
    state = session_store.get_or_create(session_id)
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


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
    if session_reset:
        response["reply"] = say(response, "The previous session is no longer available; let's review the appointment again. ",
                                "الجلسة السابقة مش موجودة؛ خلينا نراجع الموعد من جديد. ") + response["reply"]
    formatting = perf_counter()
    result = {**response, "tool_calls": traced.tool_calls, "session_reset": session_reset}
    result["timings"] = {**response.get("timings", {}), "processing_ms": round((formatting - started) * 1000, 2), "formatting_ms": round((perf_counter() - formatting) * 1000, 3)}
    return result


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
    if state.get("pending_action") is not None:
        response["pending_action"] = state["pending_action"]
    return JSONResponse(status_code=error.status_code, content=response)


# تستقبل multipart: التسجيل ومعرف الجلسة، وتعيد ChatResponse مع transcript وحقول الصوت.
# تفحص الحجم، وتفرغ التسجيل، ثم تستخدم نفس Graph والقفل والحماية مثل /api/chat.
# لا نحول كلمة yes أو نعم إلى موافقة؛ /api/approve وحدها تنفذ الكتابة.
@app.post("/api/voice", response_model=None)
async def voice(
    audio: UploadFile = File(...), session_id: str = Form(...),
    language_hint: Literal["ar", "en"] | None = Form(None), continuation: bool = Form(False),
    preview_only: bool = Form(False),
    gateway=Depends(get_gateway), adapter=Depends(get_voice_adapter),
):
    session_id = session_id.strip()
    if not session_id:
        await audio.close()
        raise HTTPException(422, "session_id must not be empty.")
    transcript = ""
    generation = uuid4().hex
    async with app.state.session_locks[session_id]:
        session_missing = continuation and not session_store.contains(session_id)
        app.state.voice_generations[session_id] = generation
        app.state.voice_reviews.pop(session_id, None)
        snapshot = state_fingerprint(session_id)
        original_state = session_store.get_or_create(session_id)
    try:
        # نقرأ حدًا أقصى معلومًا بدل تحميل ملف ضخم كاملًا في الذاكرة.
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceError(413, "The recording is too large. Please use a shorter recording (up to 10 MB).")
        digest = hashlib.sha256(data).hexdigest()
        if app.state.last_audio.get(session_id) == digest:
            raise VoiceError(409, "This recording was already sent. Please make a new recording / أعد التسجيل؛ هذا الملف أُرسل سابقًا.")
        phone_mode = bool(original_state.get("selected_slot_id") and valid_patient_name(original_state.get("patient_name")) and
                          (not original_state.get("patient_phone") or original_state.get("confirmed_phone") != original_state.get("patient_phone")))
        transcript = await adapter.transcribe(data, language_hint, phone_mode=True) if phone_mode else await adapter.transcribe(data, language_hint)
        async with app.state.session_locks[session_id]:
            if app.state.voice_generations.get(session_id) != generation or state_fingerprint(session_id) != snapshot:
                raise VoiceError(409, "A newer recording or message replaced this one / وصل تسجيل أو رسالة أحدث، أعد التسجيل.")
            token = uuid4().hex
            spoken_language = language_hint or ("ar" if any('\u0600' <= c <= '\u06ff' for c in transcript) else "en")
            app.state.voice_reviews[session_id] = {"token": token, "text": transcript, "language": spoken_language,
                "snapshot": snapshot, "generation": generation, "created": perf_counter(), "continuation": continuation, "session_missing": session_missing}
            app.state.last_audio[session_id] = digest
        if not preview_only:
            result = await confirm_voice(VoiceReviewRequest(session_id=session_id, transcript_token=token), gateway, adapter)
            if isinstance(result, dict):
                result["timings"] = {**getattr(adapter, "timings", {}), **result.get("timings", {})}
            return result
        language = spoken_language
        return {"session_id": session_id, "transcript": transcript, "transcript_token": token,
                "needs_transcript_confirmation": True, "state": original_state.get("stage"), "language": language,
                "session_reset": session_missing,
                "reply": "راجع النص اللي سمعته. إذا صحيح اضغط «استخدم النص»، أو أعد التسجيل." if language == "ar" else "Review the transcript. If correct, select Use transcript; otherwise record again.",
                "pending_action": original_state.get("pending_action"), "tool_calls": [],
                "timings": getattr(adapter, "timings", {}), **EMPTY_AUDIO}
    except VoiceError as error:
        return await voice_error_response(session_id, error, transcript, language_hint)
    except TimeoutError:
        return await voice_error_response(session_id, VoiceError(504, "The voice request took too long. Please try again."), transcript, language_hint)
    except HTTPException as error:
        return await voice_error_response(session_id, VoiceError(error.status_code, str(error.detail)), transcript, language_hint)
    except Exception:
        return await voice_error_response(session_id, VoiceError(503, "Unable to process the recording. Please try again or type your message."), transcript, language_hint)
    finally:
        await audio.close()


# لا تطبق نتيجة STT على الحالة إلا بعد مراجعة المريض للنص نفسه، وبنفس نسخة الجلسة.
@app.post("/api/voice/confirm", response_model=None)
async def confirm_voice(body: VoiceReviewRequest, gateway=Depends(get_gateway), adapter=Depends(get_voice_adapter)):
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
    # فشل TTS لا يعيد تشغيل Graph أو يعيد استخدام تسجيل قديم.
    if review["session_missing"]:
        response["session_reset"] = True
        response["reply"] = say(response, "The previous session is unavailable. ", "الجلسة السابقة مش موجودة. ") + response["reply"]
    try:
        spoken = await adapter.synthesize(response["reply"])
    except Exception:
        spoken = dict(EMPTY_AUDIO)
    return {**response, "transcript": review["text"], **spoken}


# تستقبل القرار فقط وتعيد نتيجة Approval Handler؛ لا تنشئ payload من HTTP.
# تشترك مع chat في قفل الجلسة كي لا تتغير تفاصيل الحجز أثناء الموافقة.
@app.post("/api/approve", response_model=None)
async def approve(body: ApprovalRequest, gateway=Depends(get_gateway)):
    traced = TracedGateway(gateway)
    async with app.state.session_locks[body.session_id]:
        app.state.voice_reviews.pop(body.session_id, None)
        app.state.voice_generations.pop(body.session_id, None)
        try:
            response = await handle_approval(body.session_id, body.action_id, body.approved, traced)
        except Exception:
            raise HTTPException(503, "Unable to process approval. Please try again.") from None
    return {**response, "tool_calls": traced.tool_calls}
