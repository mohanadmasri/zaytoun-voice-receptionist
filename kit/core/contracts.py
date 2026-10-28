# تذكير للشرح — contracts.py:
#
# الهدف: تحديد شكل البيانات والواجهات المشتركة بين أجزاء النظام.
#
# ToolCall: يسجل اسم الأداة ونجاحها ومدة تنفيذها.
# PendingAction: يحتوي على تفاصيل العملية التي تنتظر موافقة المريض.
# ChatResponse: يحدد شكل الرد المرسل إلى FastAPI وواجهة المستخدم.
# ClinicGateway: يحدد دوال القراءة والكتابة عبر MCP.
#
# TypedDict وProtocol يساعدان على توافق الكود，
# لكنهما لا يتحققان من صحة البيانات تلقائيًا أثناء التشغيل.
from typing import Any, Literal, NotRequired, Protocol, TypedDict

class ToolCall(TypedDict):
    tool: str
    ok: bool
    ms: float


class PendingAction(TypedDict):
    action_id: str
    type: Literal["create_booking", "cancel_booking"]
    summary: str
    payload: dict[str, Any]


class ChatResponse(TypedDict):
    session_id: str
    reply: str
    tool_calls: list[ToolCall]
    state: NotRequired[str | None]
    pending_action: NotRequired[PendingAction | None]
    result: NotRequired[dict[str, Any] | None]
    citations: NotRequired[list[dict[str, Any]]]
    usage: NotRequired[dict[str, Any]]


class ClinicGateway(Protocol):
    async def call_read(
        self, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def call_write(
        self, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]: ...
    