# انحدار أعطال العرض: Pydantic الحقيقي وAM/PM وpayload MCP مع كتابة وهمية فقط.
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError

from agent.intake import IntakeResult, extract_request
from agent.patient_identity import normalize_patient_phone, valid_patient_name
from actions.approval import prepare_booking_action, handle_approval
from core.session_store import session_store
from services.clinic_api import BookingIn, norm_phone, ApiError
from services.clinic_mcp import create_booking
import test_mandatory_features as mandatory
from test_voice_api import wav_bytes
from uuid import uuid4


# يحاكي المزود عند حد JSON نفسه؛ لا يستبدل فحص IntakeResult الذي كان يفشل في المتصفح.
def model_client(content=None, error=None):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response, side_effect=error))))


class PresentationValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_unknown_intent_is_other_only(self):
        for text in ("afternoon", "at 3 PM"):
            client = model_client(json.dumps({"language": "en", "intent": "unknown", "time_text": text}))
            with patch("agent.intake.AsyncOpenAI", return_value=client):
                result = await extract_request(text)
            self.assertEqual(result.intent, "other")
            self.assertEqual(result.time_text, text)
        for value in ("approve", "new_arbitrary_intent", None):
            with self.assertRaises(ValidationError):
                IntakeResult(intent=value)

    async def test_other_validation_and_provider_errors_remain_visible_and_private(self):
        client = model_client(json.dumps({"intent": "booking", "patient_age": "private-patient-value"}))
        with patch("agent.intake.AsyncOpenAI", return_value=client), self.assertLogs("agent.intake", level="WARNING") as captured:
            with self.assertRaises(ValidationError) as error:
                await extract_request("details")
        self.assertEqual(error.exception.errors()[0]["loc"], ("patient_age",))
        self.assertIn("int_parsing", " ".join(captured.output))
        self.assertNotIn("private-patient-value", " ".join(captured.output))
        with patch("agent.intake.AsyncOpenAI", return_value=model_client(error=RuntimeError("provider unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                await extract_request("at 3 PM")

    def test_phone_normalization_matches_original_clinic_validator(self):
        for phone in ("0599000111", "0599 000 111", "+970 599 000 111", "00972-599-000-111", "٠٥٩٩٠٠٠١١١", "۰۵۹۹۰۰۰۱۱۱"):
            self.assertEqual(normalize_patient_phone(phone), norm_phone(phone), phone)
        for phone in ("599000111", "0599", "123", "phone", "0599000111abc"):
            self.assertIsNone(normalize_patient_phone(phone), phone)
            with self.assertRaises(ApiError) as error:
                norm_phone(phone)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_phone")
        self.assertFalse(valid_patient_name("Lina " + "A" * 76))


class PresentationFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = mandatory.MandatoryFeaturesTests.asyncSetUp
    asyncTearDown = mandatory.MandatoryFeaturesTests.asyncTearDown
    send = mandatory.MandatoryFeaturesTests.send
    proposal = mandatory.MandatoryFeaturesTests.proposal

    async def test_raw_provider_json_resolves_afternoon_and_at_three_pm(self):
        # Both periods must be possible for this test to exercise the clarification reply.
        self.gateway.clinic["opening_hours"]["tuesday"]["open"] = "00:00"
        for index, reply in enumerate(("afternoon", "at 3 PM")):
            self.sid = "presentation-clock-" + uuid4().hex
            self.sessions.append(self.sid)
            await self.send("Dental checkup November 3, 2026 at 3", service_text="dental checkup", date_text="November 3, 2026", time_text="at 3")
            original = session_store.get_or_create(self.sid)
            self.assertEqual(original["time_kind"], "ambiguous")
            client = model_client(json.dumps({"language": "en", "intent": "unknown", "time_text": reply}))
            with patch("agent.intake.AsyncOpenAI", return_value=client), patch("integrations.voice_adapter.transcribe", return_value=reply):
                response = await self.client.post("/api/voice", data={"session_id": self.sid, "language_hint": "en"},
                    files={"audio": ("reply.wav", wav_bytes() + bytes([index]), "audio/wav")})
            self.assertEqual(response.status_code, 200, response.text)
            state = session_store.get_or_create(self.sid)
            self.assertEqual(state["service_id"], original["service_id"])
            self.assertEqual(state["requested_date"], original["requested_date"])
            self.assertEqual((state["time_kind"], state["requested_time"]), ("exact", "15:00"))
            self.assertNotEqual(response.json()["state"], "awaiting_time_clarification")
            self.assertEqual(self.gateway.writes, [])

    async def test_missing_zero_phone_cannot_prepare_or_write_a_booking(self):
        await self.send("Dental checkup November 3, 2026 after 3 PM for Lina Haddad phone 599000111", service_text="dental checkup",
            date_text="November 3, 2026", time_text="after 3 PM", patient_name="Lina Haddad", patient_phone="599000111", provider_any=True)
        response = await self.send("option one")
        self.assertEqual(response["state"], "awaiting_patient_details")
        self.assertIsNone(response.get("pending_action"))
        state = session_store.get_or_create(self.sid)
        state["patient_phone"] = "599000111"
        with self.assertRaises(ValueError):
            prepare_booking_action(state)
        self.assertEqual(self.gateway.writes, [])

    async def test_normalized_phone_reaches_exact_mcp_http_contract_after_approval(self):
        await self.send("Dental checkup November 3, 2026 after 3 PM for Lina Haddad phone +970 599 000 111", service_text="dental checkup",
            date_text="November 3, 2026", time_text="after 3 PM", patient_name="Lina Haddad", patient_phone="+970 599 000 111", provider_any=True)
        proposal = await self.send("option one")
        pending = proposal["pending_action"]
        self.assertEqual(pending["payload"]["patient_phone"], "0599000111")
        self.assertIn("0599000111", pending["summary"])
        captured = []
        # نختبر تحويل MCP الأصلي ونماذج العيادة، دون استدعاء endpoint كتابة فعلي أو محاكى.
        async def fake_http(method, path, **kwargs):
            body = kwargs["body"]
            model = BookingIn.model_validate(body)
            self.assertEqual(norm_phone(model.patient.phone), "0599000111")
            self.assertEqual(kwargs["headers"]["Idempotency-Key"], pending["action_id"])
            self.assertEqual(body, {"slot_id": pending["payload"]["slot_id"], "service_id": "dental_checkup",
                                  "patient": {"name": "Lina Haddad", "phone": "0599000111"}, "notes": None})
            captured.append((method, path, deepcopy(body)))
            return {"ok": True, "booking_id": "BK-MOCK-CONFIRMED"}
        async def fake_write(name, args):
            if name == "save_patient":
                return {"ok": True}
            self.assertEqual(name, "create_booking")
            with patch("services.clinic_mcp._call", new=fake_http):
                return await create_booking(**args)
        self.gateway.call_write = fake_write
        response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": pending["action_id"], "approved": True})
        self.assertEqual(response.json()["result"]["booking_id"], "BK-MOCK-CONFIRMED")
        await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": pending["action_id"], "approved": True})
        self.assertEqual(len(captured), 1)

    async def test_explicit_leading_zero_survives_wrong_llm_phone(self):
        await self.send("My phone is 0599 000 111", patient_phone="599000111")
        self.assertEqual(session_store.get_or_create(self.sid)["patient_phone"], "0599000111")

    async def test_clinic_400_does_not_turn_into_success_or_blind_retry(self):
        proposal = await self.proposal()
        self.gateway.call_write = AsyncMock(return_value={"ok": False, "status": 400, "error": {"code": "invalid_phone"}})
        response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": proposal["pending_action"]["action_id"], "approved": True})
        self.assertNotIn("result", response.json())
        self.assertIsNone(response.json().get("pending_action"))
        self.assertIn("invalid_phone", response.json()["reply"])
        await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": proposal["pending_action"]["action_id"], "approved": True})
        self.gateway.call_write.assert_awaited_once()

    async def test_success_flag_without_booking_id_is_not_confirmation(self):
        proposal = await self.proposal()
        self.gateway.call_write = AsyncMock(return_value={"ok": True})
        response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": proposal["pending_action"]["action_id"], "approved": True})
        self.assertNotIn("result", response.json())
        self.assertEqual(response.json()["state"], "booking_unconfirmed")
        self.assertEqual(response.json()["pending_action"]["action_id"], proposal["pending_action"]["action_id"])
        self.assertEqual([call.args[0] for call in self.gateway.call_write.await_args_list], ["create_booking"])

    async def test_old_invalid_pending_phone_is_rejected_before_gateway(self):
        proposal = await self.proposal()
        state = session_store.get_or_create(self.sid)
        state["patient_phone"] = "599000111"
        state["pending_action"]["payload"]["patient_phone"] = "599000111"
        session_store.save(state)
        response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": proposal["pending_action"]["action_id"], "approved": True})
        self.assertIsNone(response.json().get("pending_action"))
        self.assertNotIn("result", response.json())
        self.assertEqual(self.gateway.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
