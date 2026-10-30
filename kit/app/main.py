# واجهة FastAPI للمساعد: تربط HTTP بمنطق المحادثة والموافقة الموجود.
# المدخل JSON مطابق للعقد؛ المخرج ChatResponse أو خطأ تحقق/تعذر خدمة.
# يصل LLM عبر handle_message، وتصل أدوات MCP عبر Gateway فقط.
# لا ينفذ مسار chat كتابة؛ الكتابة محصورة بمنطق handle_approval الموثوق.
# الحالة والأقفال في RAM؛ شغّل worker واحدًا حتى إضافة تخزين مشترك.
# الصوت والنص يشتركان في process_message؛ الكتابة تبقى في مسار الموافقة فقط.
import asyncio
from collections import defaultdict
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from actions.approval import handle_approval
from agent.runner import handle_message
from core.contracts import ClinicGateway as GatewayProtocol
from integrations.mcp_gateway import ClinicGateway
from integrations.voice_adapter import VoiceAdapter, VoiceError, MAX_AUDIO_BYTES, EMPTY_AUDIO
from core.session_store import session_store


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# يتحقق Pydantic من طلب المحادثة قبل وصوله إلى Graph.
# user بيانات اختيارية حسب العقد، ولا تُستخدم لتجاوز استخراج بيانات المريض.
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: NonEmptyText
    message: NonEmptyText
    user: dict[str, Any] | None = None


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
        return await self._call(self.gateway.call_read, tool_name, args)

    # تمرر كتابة من Approval Handler فقط؛ لا تمنح LLM صلاحية كتابة جديدة.
    async def call_write(self, tool_name: str, args: dict) -> dict:
        return await self._call(self.gateway.call_write, tool_name, args)


app = FastAPI(title="Zaytoun Voice Receptionist")
app.state.session_locks = defaultdict(asyncio.Lock)


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
async def process_message(session_id: str, message: str, gateway):
    traced = TracedGateway(gateway)
    async with app.state.session_locks[session_id]:
        try:
            response = await handle_message(session_id, message, traced)
        except Exception:
            # لا نعيد نص الاستثناء الذي قد يحتوي معلومات داخلية أو حساسة.
            raise HTTPException(503, "Unable to process the message. Please try again.") from None
    return {**response, "tool_calls": traced.tool_calls}


# تستقبل JSON المفحوص بـPydantic وتستخدم نفس المعالجة التي يستدعيها مسار الصوت.
@app.post("/api/chat", response_model=None)
async def chat(body: ChatRequest, gateway=Depends(get_gateway)):
    return await process_message(body.session_id, body.message, gateway)


# تعيد خطأ صوتيًّا واضحًا مع حالة الجلسة وموافقتها الحالية دون تغييرهما.
# تبقى أزرار الموافقة ظاهرة إذا فشل التسجيل بعد تجهيز طلب سابق.
async def voice_error_response(session_id: str, error: VoiceError, transcript: str = ""):
    async with app.state.session_locks[session_id]:
        state = session_store.get_or_create(session_id)
    response = {"session_id": session_id, "reply": str(error), "tool_calls": [],
                "state": state.get("stage"), "transcript": transcript, **EMPTY_AUDIO}
    if state.get("pending_action") is not None:
        response["pending_action"] = state["pending_action"]
    return JSONResponse(status_code=error.status_code, content=response)


# تستقبل multipart: التسجيل ومعرف الجلسة، وتعيد ChatResponse مع transcript وحقول الصوت.
# تفحص الحجم، وتفرغ التسجيل، ثم تستخدم نفس Graph والقفل والحماية مثل /api/chat.
# لا نحول كلمة yes أو نعم إلى موافقة؛ /api/approve وحدها تنفذ الكتابة.
@app.post("/api/voice", response_model=None)
async def voice(
    audio: UploadFile = File(...), session_id: str = Form(...),
    gateway=Depends(get_gateway), adapter=Depends(get_voice_adapter),
):
    session_id = session_id.strip()
    if not session_id:
        await audio.close()
        raise HTTPException(422, "session_id must not be empty.")
    transcript = ""
    try:
        # نقرأ حدًا أقصى معلومًا بدل تحميل ملف ضخم كاملًا في الذاكرة.
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceError(413, "The recording is too large. Please use a shorter recording (up to 10 MB).")
        transcript = await adapter.transcribe(data)
        response = await asyncio.wait_for(process_message(session_id, transcript, gateway), timeout=60)
    except VoiceError as error:
        return await voice_error_response(session_id, error, transcript)
    except TimeoutError:
        return await voice_error_response(session_id, VoiceError(504, "The voice request took too long. Please try again."), transcript)
    except HTTPException as error:
        return await voice_error_response(session_id, VoiceError(error.status_code, str(error.detail)), transcript)
    except Exception:
        return await voice_error_response(session_id, VoiceError(503, "Unable to process the recording. Please try again or type your message."), transcript)
    finally:
        await audio.close()
    # القراءة أو تجهيز الموافقة اكتملت؛ فشل TTS يجب ألا يعيد معالجة الرسالة.
    try:
        spoken = await adapter.synthesize(response["reply"])
    except Exception:
        spoken = dict(EMPTY_AUDIO)
    return {**response, "transcript": transcript, **spoken}


# تستقبل القرار فقط وتعيد نتيجة Approval Handler؛ لا تنشئ payload من HTTP.
# تشترك مع chat في قفل الجلسة كي لا تتغير تفاصيل الحجز أثناء الموافقة.
@app.post("/api/approve", response_model=None)
async def approve(body: ApprovalRequest, gateway=Depends(get_gateway)):
    traced = TracedGateway(gateway)
    async with app.state.session_locks[body.session_id]:
        try:
            response = await handle_approval(body.session_id, body.action_id, body.approved, traced)
        except Exception:
            raise HTTPException(503, "Unable to process approval. Please try again.") from None
    return {**response, "tool_calls": traced.tool_calls}
