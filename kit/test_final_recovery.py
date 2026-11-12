# إعادة إنتاج أعطال التصحيح قبل الإصلاح؛ لا صوت حقيقي أو كتابة في عيادة عاملة.
import unittest
from core.session_store import session_store
from agent.patient_identity import normalize_patient_phone
import test_mandatory_features as mandatory


class FinalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = mandatory.MandatoryFeaturesTests.asyncSetUp
    asyncTearDown = mandatory.MandatoryFeaturesTests.asyncTearDown
    async def send(self, message, **fields):
        return await mandatory.MandatoryFeaturesTests.send(self, message, confirm_phone=False, **fields)

    def state(self):
        return session_store.get_or_create(self.sid)

    async def selected(self):
        await self.send("Dental checkup November 3, 2026 after 3 PM", service_text="dental checkup", date_text="November 3, 2026", time_text="after 3 PM", provider_any=True)
        await self.send("option one")

    async def test_phone_requires_confirmation_then_typed_correction_keeps_selection(self):
        await self.selected()
        await self.send("My name is Lina Haddad", patient_name="Lina Haddad")
        selected = self.state()["selected_slot_id"]
        for number in ("05990001111", "05880001111", "005899001111", "058900111", "0589001111"):
            data = await self.send(number, patient_phone=number)
            self.assertEqual(self.state()["selected_slot_id"], selected)
            self.assertEqual(self.state()["patient_name"], "Lina Haddad")
            self.assertIsNone(data.get("pending_action"))
            if normalize_patient_phone(number):
                self.assertEqual(data["state"], "awaiting_phone_confirmation")
                self.assertIn(number, data["reply"])
        data = await self.send("Yes, that phone number is correct")
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(self.gateway.writes, [])

    async def test_noise_while_collecting_name_keeps_the_selected_slot(self):
        await self.selected()
        original = self.state()
        for noise in ("00:08", "nine"):
            data = await self.send(noise, patient_name=noise, time_text="00:08" if ":" in noise else None)
            self.assertEqual(self.state()["selected_slot_id"], original["selected_slot_id"])
            self.assertEqual(self.state()["requested_date"], original["requested_date"])
            self.assertEqual(self.state()["service_id"], original["service_id"])
            self.assertEqual(data["state"], "awaiting_patient_details")
        await self.send("My name is Lina Haddad", patient_name="Lina Haddad")
        self.assertEqual(self.state()["patient_name"], "Lina Haddad")
        self.assertEqual(self.state()["selected_slot_id"], original["selected_slot_id"])

    async def test_unknown_provider_does_not_trap_current_option_selection(self):
        await self.selected()
        state = self.state()
        state.update({"selected_slot_id": None, "stage": "slots_proposed"})
        session_store.save(state)
        offered = self.state()["proposed_slots"]
        await self.send("I want Dr. Misheard", provider_text="Dr. Misheard")
        self.assertEqual(self.state()["proposed_slots"], offered)
        await self.send("option one")
        self.assertEqual(self.state()["selected_slot_id"], offered[0]["slot_id"])

    async def test_unknown_provider_can_recover_by_current_doctor_name(self):
        await self.selected()
        state = self.state()
        state.update({"selected_slot_id": None, "stage": "slots_proposed"})
        session_store.save(state)
        offered = state["proposed_slots"]
        await self.send("I want Dr. Misheard", provider_text="Dr. Misheard")
        await self.send("Dr. Rania Khoury", provider_text="Dr. Rania Khoury")
        self.assertEqual(self.state()["proposed_slots"], offered)
        self.assertTrue(self.state().get("selected_slot_id") or self.state().get("selection_candidates"))
        self.assertTrue(all(s["provider_id"] == "dr_khoury" for s in offered if s["slot_id"] in self.state().get("selection_candidates", [])))

    async def test_single_option_deictic_selection(self):
        await self.selected()
        state = self.state()
        state.update({"selected_slot_id": None, "stage": "slots_proposed", "proposed_slots": state["proposed_slots"][:1]})
        session_store.save(state)
        data = await self.send("I want this option")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertEqual(self.state()["selected_slot_id"], state["proposed_slots"][0]["slot_id"])
        for message in ("that appointment!", "بدي هاد الموعد", "I want this option."):
            state.update({"selected_slot_id": None, "stage": "slots_proposed"})
            session_store.save(state)
            await self.send(message)
            self.assertEqual(self.state()["selected_slot_id"], state["proposed_slots"][0]["slot_id"])

    async def test_phone_rejection_and_changed_number_invalidate_old_approval_only(self):
        await self.selected()
        await self.send("My name is Lina Haddad", patient_name="Lina Haddad")
        await self.send("0599000111", patient_phone="0599000111")
        await self.send("no")
        self.assertIsNone(self.state()["patient_phone"])
        self.assertEqual(self.state()["patient_name"], "Lina Haddad")
        selected = self.state()["selected_slot_id"]
        await self.send("zero five nine nine zero zero zero one one one", patient_phone="599000111")
        self.assertEqual(self.state()["patient_phone"], "0599000111")
        ready = await self.send("yes")
        old_id = ready["pending_action"]["action_id"]
        changed = await self.send("My phone is 0599000222", patient_phone="0599000222")
        self.assertEqual(changed["state"], "awaiting_phone_confirmation")
        self.assertIsNone(changed.get("pending_action"))
        self.assertEqual(self.state()["selected_slot_id"], selected)
        ready = await self.send("yes")
        self.assertNotEqual(ready["pending_action"]["action_id"], old_id)
        self.assertEqual(self.gateway.writes, [])

    async def test_voice_phone_context_and_confirmation_do_not_book(self):
        await self.selected()
        await self.send("Lina Haddad", patient_name="Lina Haddad")
        from unittest.mock import patch
        from agent.intake import IntakeResult
        from test_voice_api import wav_bytes
        from uuid import uuid4
        for transcript, expected in (("صفر خمسة تسعة تسعة صفر صفر صفر واحد واحد واحد", "awaiting_phone_confirmation"), ("نعم", "awaiting_approval")):
            async def extract(_message):
                return IntakeResult(language="ar", intent="other")
            with patch("integrations.voice_adapter.transcribe", return_value=transcript) as stt, patch("agent.graph.extract_request", new=extract):
                response = await self.client.post("/api/voice", data={"session_id": self.sid, "language_hint": "ar"},
                    files={"audio": ("digits.wav", wav_bytes() + uuid4().bytes, "audio/wav")})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["state"], expected)
            self.assertTrue(stt.call_args.kwargs["phone_mode"])
            self.assertEqual(self.state()["patient_phone"], "0599000111")
            self.assertEqual(self.gateway.writes, [])

    async def test_policy_rejection_clears_approval_and_never_retries_old_action(self):
        await self.selected()
        await self.send("Lina Haddad", patient_name="Lina Haddad")
        await self.send("0599000111", patient_phone="0599000111")
        ready = await self.send("yes")
        action = ready["pending_action"]["action_id"]
        from unittest.mock import AsyncMock, patch
        failure = {"ok": False, "status": 422, "error": {"code": "policy_violation", "message": "A patient under 16 needs a parent or guardian: send patient.guardian_name."}}
        with patch.object(self.gateway, "call_write", new=AsyncMock(return_value=failure)) as write:
            response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": action, "approved": True})
            data = response.json()
            self.assertIsNone(data.get("result"))
            self.assertIsNone(data.get("pending_action"))
            self.assertIn("patient.guardian_name", data["validation_error"]["rejected_fields"])
            await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": action, "approved": True})
            self.assertEqual(write.await_count, 1)

    async def test_capacity_policy_prevents_booking_write(self):
        await self.selected()
        await self.send("Lina Haddad", patient_name="Lina Haddad")
        await self.send("0599000111", patient_phone="0599000111")
        ready = await self.send("yes")
        state = self.state()
        state.setdefault("clinic_calendar", {})["policies"] = {"max_upcoming_bookings_per_patient": 3}
        session_store.save(state)
        from unittest.mock import AsyncMock, patch
        clinic_read = self.gateway.call_read

        async def read_with_full_patient(tool, arguments):
            if tool == "get_patient":
                return {"ok": True, "upcoming_bookings": [{}, {}, {}]}
            return await clinic_read(tool, arguments)

        with patch.object(self.gateway, "call_read", new=AsyncMock(side_effect=read_with_full_patient)):
            response = await self.client.post("/api/approve", json={"session_id": self.sid, "action_id": ready["pending_action"]["action_id"], "approved": True})
        self.assertIsNone(response.json().get("pending_action"))
        self.assertIsNone(response.json().get("result"))
        self.assertEqual(self.gateway.writes, [])

    async def test_clarifying_thursday_resolves_conflict_and_invalidates_old_slots(self):
        await self.selected()
        await self.send("Thursday, November 3, 2026", date_text="Thursday, November 3, 2026")
        self.assertTrue(self.state().get("date_conflict"))
        await self.send("I mean Thursday", date_text="Thursday")
        self.assertIsNone(self.state().get("date_conflict"))
        self.assertEqual(self.state()["requested_date"], "2026-11-05")
        self.assertIsNone(self.state()["selected_slot_id"])
        self.assertTrue(all(slot["date"] == "2026-11-05" for slot in self.state()["proposed_slots"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
