# يختبر HTTP مع Graph والموافقة الحقيقيين، ويستبدل LLM وMCP ببدائل محلية.
# المدخل طلبات JSON؛ المخرج تحقق من الرد والحالة وعدد الكتابات.
# لا يتصل بأي API خارجي ولا ينشئ حجزًا في العيادة؛ كل البيانات وهمية.
import asyncio
from unittest.mock import patch

import httpx

from app.main import app, get_gateway
from agent.intake import IntakeResult
from agent.state import create_initial_state
from core.session_store import session_store


# بوابة اختبار ترجع مواعيد ثابتة، وتسجل الكتابات، وتحاكي النجاح و409 و503.
class FakeGateway:
    def __init__(self):
        self.writes = []
        self.failure = None

    async def call_read(self, name, args):
        if name == "clinic_info":
            return {"ok": True, "today": "2026-11-02"}
        if name == "find_slots":
            assert args["after"] == "15:01"
            return {"ok": True, "slots": [slot()]}
        raise AssertionError(f"Unexpected read: {name}")

    async def call_write(self, name, args):
        assert name == "create_booking"
        self.writes.append(args)
        if self.failure:
            return {"ok": False, "error": {"code": self.failure}}
        return {"ok": True, "booking_id": "BK-API-TEST"}


# تعيد موعدًا وهميًا واحدًا صالحًا للطلب كي لا يعتمد الاختبار على تقويم العيادة.
def slot():
    return {"slot_id": "SL-API-TEST", "date": "2026-11-03", "start": "15:30",
            "service_id": "dental_checkup", "provider_id": "dr_khoury",
            "provider_name": "Dr. Rania Khoury"}


# تهيئ جلسة اختبار مستقلة وتعيد معرفها؛ يحذف main الجلسات في النهاية.
def seed(label):
    session_id = f"api-test-{label}"
    state = create_initial_state(session_id)
    state.update({"language": "en", "intent": "booking", "provider_choice": "any",
                  "service_text": "dental checkup", "service_id": "dental_checkup",
                  "date_text": "November 3, 2026", "requested_date": "2026-11-03",
                  "time_text": "after 3 PM", "time_kind": "window", "time_after": "15:01",
                  "patient_name": "Test Patient", "patient_phone": "0599000999",
                  "stage": "slots_proposed", "proposed_slots": [slot()]})
    session_store.save(state)
    return session_id


# تستبدل استخراج LLM فقط، ثم ترسل طلب HTTP يمر عبر Graph والمخزن الحقيقيين.
async def choose(client, session_id):
    async def fake_extract(message):
        return IntakeResult(language="en", intent="booking")

    with patch("agent.graph.extract_request", new=fake_extract):
        response = await client.post("/api/chat", json={
            "session_id": session_id, "message": "I choose SL-API-TEST", "user": {}})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "awaiting_approval", data
    assert data.get("result") is None
    return data["pending_action"]["action_id"]


# تختبر الرحلة والرفض والتكرار والتزامن وفشل الأدوات والتحقق من الموافقة.
# ASGITransport يشغّل HTTP داخل العملية دون فتح منفذ أو اتصال شبكي خارجي.
async def main():
    gateway = FakeGateway()
    app.dependency_overrides[get_gateway] = lambda: gateway
    sessions = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            for payload in ({"session_id": " ", "message": "hi"},
                            {"session_id": "test", "message": " "}):
                assert (await client.post("/api/chat", json=payload)).status_code == 422
            for payload in ({"session_id": "test", "action_id": "x", "approved": "true"},
                            {"session_id": "test", "action_id": "x", "approved": True, "payload": {}}):
                assert (await client.post("/api/approve", json=payload)).status_code == 422
            assert gateway.writes == []
            print("PASSED: health and strict request validation")

            session_id = seed("success")
            sessions.append(session_id)
            action_id = await choose(client, session_id)
            assert gateway.writes == []
            payload = {"session_id": session_id, "action_id": action_id, "approved": True}
            responses = await asyncio.gather(*(client.post("/api/approve", json=payload) for _ in range(2)))
            assert all(response.status_code == 200 for response in responses)
            assert len(gateway.writes) == 1
            assert gateway.writes[0]["idempotency_key"] == action_id
            success = next(response.json() for response in responses if response.json().get("result"))
            assert success["result"]["booking_id"] == "BK-API-TEST"
            assert success["tool_calls"][0]["tool"] == "create_booking"
            print("PASSED: chat prepares approval; concurrent approvals write once")

            session_id = seed("reject")
            sessions.append(session_id)
            action_id = await choose(client, session_id)
            unknown = await client.post("/api/approve", json={
                "session_id": session_id, "action_id": "wrong-id", "approved": True})
            assert unknown.json().get("result") is None
            rejected = await client.post("/api/approve", json={
                "session_id": session_id, "action_id": action_id, "approved": False})
            assert rejected.json()["state"] == "booking_rejected"
            assert len(gateway.writes) == 1
            print("PASSED: unknown action and rejection write nothing")

            for failure, stage in (("slot_taken", "slot_unavailable"), ("unreachable", "booking_unconfirmed")):
                session_id = seed(failure)
                sessions.append(session_id)
                action_id = await choose(client, session_id)
                gateway.failure = failure
                response = await client.post("/api/approve", json={
                    "session_id": session_id, "action_id": action_id, "approved": True})
                data = response.json()
                assert data["state"] == stage, data
                assert data.get("result") is None
                assert data["tool_calls"][0]["ok"] is False
                assert "BK-" not in data["reply"]
            gateway.failure = None
            print("PASSED: tool conflict and unavailability do not claim booking success")

            session_id = seed("search")
            sessions.append(session_id)
            state = session_store.get_or_create(session_id)
            state.update({"stage": "intake", "proposed_slots": [], "patient_phone": None})
            session_store.save(state)
            async def fake_extract(message):
                return IntakeResult(language="en", intent="booking")
            with patch("agent.graph.extract_request", new=fake_extract):
                response = await client.post("/api/chat", json={"session_id": session_id, "message": "Find appointments"})
            data = response.json()
            assert data["state"] == "slots_proposed", data
            assert [call["tool"] for call in data["tool_calls"]] == ["clinic_info", "find_slots"]
            assert all(call["ok"] and call["ms"] >= 0 for call in data["tool_calls"])
            print("PASSED: HTTP response traces all tools, including find_slots")

            async def failed_message(*args):
                raise RuntimeError("private internal details")
            with patch("app.main.handle_message", new=failed_message):
                response = await client.post("/api/chat", json={"session_id": session_id, "message": "hi"})
            assert response.status_code == 503
            assert "private internal details" not in response.text
            print("PASSED: unexpected errors return 503 without internal details")
    finally:
        app.dependency_overrides.clear()
        for session_id in sessions:
            session_store.delete(session_id)
            app.state.session_locks.pop(session_id, None)
    print("ALL API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
