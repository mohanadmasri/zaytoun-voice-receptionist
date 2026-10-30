# واجهة FastAPI للمساعد: تربط HTTP بمنطق المحادثة والموافقة الموجود.
# المدخل JSON مطابق للعقد؛ المخرج ChatResponse أو خطأ تحقق/تعذر خدمة.
# يصل LLM عبر handle_message، وتصل أدوات MCP عبر Gateway فقط.
# لا ينفذ مسار chat كتابة؛ الكتابة محصورة بمنطق handle_approval الموثوق.
# الحالة والأقفال في RAM؛ شغّل worker واحدًا حتى إضافة تخزين مشترك.
import asyncio
from collections import defaultdict
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from actions.approval import handle_approval
from agent.runner import handle_message
from core.contracts import ClinicGateway as GatewayProtocol
from integrations.mcp_gateway import ClinicGateway


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


# تعيد حالة خدمة HTTP فقط؛ لا تستدعي LLM أو العيادة ولا تؤكد جاهزيتهما.
@app.get("/health")
async def health():
    return {"status": "ok"}


# تستقبل رسالة متحققًا منها وتعيد استجابة Graph مع سجل أدوات الجولة كاملًا.
# القفل يشمل قراءة الحالة ومعالجتها وحفظها لمنع ضياع تحديثات الجلسة المتزامنة.
@app.post("/api/chat", response_model=None)
async def chat(body: ChatRequest, gateway=Depends(get_gateway)):
    traced = TracedGateway(gateway)
    async with app.state.session_locks[body.session_id]:
        try:
            response = await handle_message(body.session_id, body.message, traced)
        except Exception:
            # لا نعيد نص الاستثناء الذي قد يحتوي معلومات داخلية أو حساسة.
            raise HTTPException(503, "Unable to process the message. Please try again.") from None
    return {**response, "tool_calls": traced.tool_calls}


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
