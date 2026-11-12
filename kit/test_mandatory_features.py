# انحدار المتطلبات الناقصة: الصوت التلقائي والذاكرة وولي الأمر والإلغاء؛ كل الكتابات وهمية.
import unittest
from uuid import uuid4
from unittest.mock import patch
import httpx
from app.main import app, get_gateway
from agent.intake import IntakeResult
from core.session_store import session_store
from evals.run_safe_evals import EvalGateway
from test_voice_api import wav_bytes
from test_voice_ui import app_ui
from test_support import confirm_phone_turn


class MandatoryFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sid = "mandatory-" + uuid4().hex
        self.sessions = [self.sid]
        self.gateway = EvalGateway()
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.pop(get_gateway, None)
        for sid in self.sessions:
            session_store.delete(sid)
            for store in (app.state.session_locks, app.state.voice_reviews, app.state.voice_generations, app.state.last_audio):
                store.pop(sid, None)

    async def send(self, message, voice=False, confirm_phone=True, **fields):
        async def extract(_message):
            return IntakeResult(language="en", intent="booking", **fields)
        with patch("agent.graph.extract_request", new=extract), patch("integrations.voice_adapter.transcribe", return_value=message):
            if voice:
                response = await self.client.post("/api/voice", data={"session_id": self.sid, "language_hint": "en"},
                    files={"audio": ("voice.wav", wav_bytes() + uuid4().bytes, "audio/wav")})
            else:
                response = await self.client.post("/api/chat", json={"session_id": self.sid, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        if confirm_phone:
            response = await confirm_phone_turn(self.client, response, self.sid)
        return response.json()

    async def proposal(self, voice=False):
        await self.send("Dental checkup on November 3, 2026 after 3 PM for Lina Haddad, phone 0599000111", voice=voice,
                        service_text="dental checkup", date_text="November 3, 2026", time_text="after 3 PM",
                        patient_name="Lina Haddad", patient_phone="0599000111", provider_any=True)
        return await self.send("option one", voice=voice)

    async def test_automatic_voice_does_not_need_transcript_buttons_or_book(self):
        data = await self.proposal(voice=True)
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertNotIn("transcript_token", data)
        self.assertEqual(data["transcript"], "option one")
        action = data["pending_action"]
        for value in ("Lina Haddad", "0599000111", "November 3", "3:30", "Rania"):
            self.assertIn(value, action["summary"])
        data = await self.send("yes", voice=True)
        self.assertEqual(data["pending_action"]["action_id"], action["action_id"])
        self.assertEqual(self.gateway.writes, [])

    async def test_voice_identity_change_preserves_current_approved_details(self):
        original = await self.proposal()
        state = session_store.get_or_create(self.sid)
        response = await self.send("This appointment is for my son", voice=True, patient_name="my son")
        self.assertEqual(response["pending_action"]["action_id"], original["pending_action"]["action_id"])
        after = session_store.get_or_create(self.sid)
        for field in ("patient_name", "patient_phone", "selected_slot_id", "pending_action"):
            self.assertEqual(after[field], state[field])
        self.assertIn("type", response["reply"])
        self.assertEqual(self.gateway.writes, [])

    async def test_patient_memory_survives_a_different_agent_session(self):
        proposal = await self.proposal()
        approved = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": proposal["pending_action"]["action_id"], "approved": True})
        booking_id = approved.json()["appointment"]["booking_id"]
        self.assertEqual([name for name, _ in self.gateway.writes], ["create_booking", "save_patient"])
        self.sid = "mandatory-returning-" + uuid4().hex
        self.sessions.append(self.sid)
        await self.send("Dental checkup on November 4, 2026 after 3 PM. Phone 0599000111", service_text="dental checkup",
                        date_text="November 4, 2026", time_text="after 3 PM", patient_phone="0599000111")
        state = session_store.get_or_create(self.sid)
        self.assertEqual(state["patient_lookup_status"], "found")
        self.assertIsNone(state["patient_name"])
        self.assertEqual(self.gateway.patients["0599000111"]["language"], "en")
        for phone in ("0599000111", "0599000999"):
            with self.subTest(phone=phone):
                if phone != "0599000111":
                    self.sid = "mandatory-unknown-" + uuid4().hex
                    self.sessions.append(self.sid)
                    await self.send(f"Book a dental checkup, phone {phone}", service_text="dental checkup", patient_phone=phone)
                result = await self.send("Show my bookings.", confirm_phone=False)
                self.assertEqual(result["session_appointments"], [])
                self.assertNotIn(booking_id, result["reply"])
                self.assertNotIn("Lina Haddad", result["reply"])
                self.assertIn("verify your identity", result["reply"])
                self.assertEqual(result["tool_calls"], [])
                attempt = await self.client.post("/api/approve", json={"session_id": self.sid,
                    "action_id": proposal["pending_action"]["action_id"], "approved": True})
                self.assertIsNone(attempt.json().get("appointment"))
        self.assertEqual(len(self.gateway.writes), 2)

    async def test_appointment_history_preserves_booking_context_and_uses_private_speech(self):
        from integrations.clinic_speech import tickets

        proposed = await self.proposal()
        self.assertEqual(self.gateway.writes, [])
        action = proposed["pending_action"]["action_id"]
        approved = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": action, "approved": True})
        booking = approved.json()["session_appointments"][0]
        for question in ("شو مواعيدي المحجوزة؟", "شو المواعيد اللي أنا عندي فيها موعد؟",
                         "شو آخر موعد حجزته؟", "شو مواعيدي؟", "عندي حجز؟", "ورجيني حجوزاتي",
                         "What are my appointments?", "What did I book last?", "Show my bookings."):
            with self.subTest(question=question):
                state = session_store.get_or_create(self.sid)
                result = await self.send(question, confirm_phone=False)
                self.assertIn(booking["booking_id"], result["reply"])
                self.assertEqual(result["session_appointments"], [booking])
                self.assertEqual(result["tool_calls"], [])
                self.assertEqual(session_store.get_or_create(self.sid), state)
                self.assertNotIn(booking["booking_id"], tickets.pop(result["speech_token"])[1])
        await self.send("Dental checkup on November 4, 2026 after 3 PM", service_text="dental checkup",
                        date_text="November 4, 2026", time_text="after 3 PM")
        offered = session_store.get_or_create(self.sid)
        self.assertTrue(offered["proposed_slots"])
        await self.send("What are your clinic hours?")
        before = session_store.get_or_create(self.sid)
        self.assertEqual(before["proposed_slots"], offered["proposed_slots"])
        result = await self.send("Show my appointments", voice=True, confirm_phone=False)
        self.assertIn(booking["booking_id"], result["reply"])
        self.assertEqual(session_store.get_or_create(self.sid), before)
        await self.send("option one")
        selected = session_store.get_or_create(self.sid)
        self.assertIsNotNone(selected["selected_slot_id"])
        await self.send("My name is Sample Patient", patient_name="Sample Patient")
        collecting = session_store.get_or_create(self.sid)
        result = await self.send("What did I book last?", voice=True, confirm_phone=False)
        self.assertIn(booking["booking_id"], result["reply"])
        self.assertEqual(session_store.get_or_create(self.sid), collecting)
        self.assertEqual(len(self.gateway.writes), 2)

    async def test_confirmed_appointment_history_updates_on_cancellation(self):
        proposed = await self.proposal()
        approved = await self.client.post("/api/approve", json={"session_id": self.sid,
            "action_id": proposed["pending_action"]["action_id"], "approved": True})
        booking = approved.json()["session_appointments"][0]
        booking_id = booking["booking_id"]
        self.gateway.bookings[booking_id].update(
            booking, start_at=f"{booking['date']}T{booking['start']}:00+02:00")
        transcript = f"Cancel {booking_id}, my phone is 0599000111"
        with patch("agent.graph.extract_request", return_value=IntakeResult(
                language="en", intent="cancel_booking", cancellation_booking_id=booking_id,
                patient_phone="0599000111")):
            review = await self.client.post("/api/chat", json={"session_id": self.sid, "message": transcript})
        self.assertEqual(review.json()["session_appointments"], [booking])
        self.assertEqual(len(self.gateway.writes), 2)
        cancelled = await self.client.post("/api/approve", json={"session_id": self.sid,
            "action_id": review.json()["pending_action"]["action_id"], "approved": True})
        self.assertEqual(cancelled.json()["session_appointments"], [{**booking, "status": "cancelled"}])
        history = await self.send("What did I book last?", confirm_phone=False)
        self.assertIn("Cancelled", history["reply"])
        self.assertIn(booking_id, history["reply"])
        self.assertEqual([tool for tool, _ in self.gateway.writes], ["create_booking", "save_patient", "cancel_booking"])
        new_request = await self.send("Book a dental checkup on November 4, 2026 after 3 PM",
                                      service_text="dental checkup", date_text="November 4, 2026", time_text="after 3 PM")
        self.assertEqual(new_request["state"], "slots_proposed")
        self.assertEqual(new_request["session_appointments"], [{**booking, "status": "cancelled"}])
        self.assertIsNone(new_request.get("pending_action"))
        self.assertEqual(len(self.gateway.writes), 3)

    async def test_cancellation_ownership_and_policy_fail_without_write(self):
        self.gateway = EvalGateway({"cancellation": True, "too_late": True})
        for phone in ("0599000999", "0599000111"):
            async def extract(_message):
                return IntakeResult(language="en", intent="cancel_booking", cancellation_booking_id="BK-EXISTING", patient_phone=phone)
            with patch("agent.graph.extract_request", new=extract):
                response = await self.client.post("/api/chat", json={"session_id": self.sid, "message": f"Cancel BK-EXISTING phone {phone}"})
            self.assertIsNone(response.json().get("pending_action"))
            self.assertEqual(self.gateway.writes, [])

    def test_ui_events_are_automatic_and_diagnostics_absent(self):
        config = app_ui.demo.config
        targets = [target for dependency in config["dependencies"] for target in dependency.get("targets", [])]
        self.assertIn((app_ui.mic._id, "stop_recording"), [tuple(t) for t in targets])
        labels = [component.get("props", {}).get("value") for component in config["components"] if component["type"] == "button"]
        self.assertFalse(any(isinstance(label, str) and ("Use transcript" in label or "Transcribe current" in label) for label in labels))
        accordion = [component for component in config["components"] if component["type"] == "accordion"]
        self.assertFalse(accordion)


if __name__ == "__main__":
    unittest.main(verbosity=2)
