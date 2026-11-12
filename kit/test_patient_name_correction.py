import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from actions.approval import handle_approval
from agent.intake import IntakeResult
from agent.runner import handle_message
from agent.state import create_initial_state
from core.session_store import SessionStore
from integrations.voice_adapter import VoiceAdapter, VoiceError
from test_stabilization_booking import booking_state
from test_voice_api import wav_bytes


class PatientNameCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_context_passes_explicit_name_correction_to_preview(self):
        adapter = VoiceAdapter()
        correction = "لا، اسمي سارة الخالدي"
        with patch("integrations.voice_adapter.transcribe", return_value=correction):
            self.assertEqual(await adapter.transcribe(wav_bytes(), "ar", phone_mode=True), correction)
        with patch("integrations.voice_adapter.transcribe", return_value="رقمي ٠٥٩٩٠٠١١١"):
            with self.assertRaises(VoiceError):
                await adapter.transcribe(wav_bytes(), "ar", phone_mode=True)

    async def test_recognized_name_and_explicit_corrections_flow_to_review(self):
        for phrase, corrected, language in (
            ("لا، اسمي سارة الخالدي", "سارة الخالدي", "ar"),
            ("الاسم الصحيح سارة النجار", "سارة النجار", "ar"),
            ("مش سارة الحداد، اسمي سارة المصري", "سارة المصري", "ar"),
            ("I meant Sara Khalidi", "Sara Khalidi", "en"),
            ("My correct name is Sara Al-Najjar", "Sara Al-Najjar", "en"),
        ):
            with self.subTest(phrase=phrase):
                await self.check_correction(phrase, corrected, language)

    async def check_correction(self, phrase, corrected, language):
        session = "name-correction"
        store = SessionStore()
        state = {**create_initial_state(session), **booking_state(
            stage="awaiting_patient_details", selected_slot_id="SL-1",
        )}
        store.save(state)
        gateway = SimpleNamespace(call_read=AsyncMock(), call_write=AsyncMock())
        extract = AsyncMock(return_value=IntakeResult(
            language="ar", intent="booking", patient_name="سارة النجار",
        ))
        with patch("agent.runner.session_store", store), patch("agent.graph.extract_request", extract):
            details = await handle_message(session, "اسمي سارة الحداد", gateway, input_language="ar")
            self.assertEqual(details["state"], "awaiting_patient_details")
            self.assertEqual(store.get_or_create(session)["patient_name"], "سارة الحداد")
            extract.return_value = IntakeResult(
                language="ar", intent="booking", patient_phone="0599000111", patient_name="سارة النجار",
            )
            phone = await handle_message(session, "0599000111", gateway, input_language="ar")
            self.assertEqual(phone["state"], "awaiting_phone_confirmation")
            self.assertNotIn("pending_action", phone)
            extract.return_value = IntakeResult(language="ar", intent="other", patient_name="سارة النجار")
            review = await handle_message(session, "نعم", gateway, input_language="ar")
            previous = review["pending_action"]
            self.assertEqual(previous["payload"]["patient_name"], "سارة الحداد")

            extract.return_value = IntakeResult(
                language=language, intent="other",
                patient_name=corrected if phrase.startswith("I meant") else "Incorrect Surname",
                service_text="different service", date_text="tomorrow", time_text="10:00",
                provider_text="different doctor", patient_phone="0599000222",
            )
            review = await handle_message(session, phrase, gateway, input_language=language)
            current = review["pending_action"]
            self.assertEqual(review["state"], "awaiting_approval")
            self.assertNotEqual(previous["action_id"], current["action_id"])
            self.assertIn(corrected, current["summary"])
            self.assertIn(corrected, review["reply"])
            self.assertEqual(current["payload"], {**previous["payload"], "patient_name": corrected})
            saved = store.get_or_create(session)
            self.assertEqual(saved["confirmed_phone"], "0599000111")
            self.assertEqual(saved["requested_date"], state["requested_date"])
            self.assertEqual(saved["proposed_slots"], state["proposed_slots"])

            with patch("actions.approval.session_store", store):
                stale = await handle_approval(session, previous["action_id"], True, gateway)
            self.assertEqual(stale["pending_action"]["action_id"], current["action_id"])

            extract.return_value = IntakeResult(language=language, intent="other", patient_name="Incorrect Surname")
            response = await handle_message(session, "تمام" if language == "ar" else "Okay", gateway)
            self.assertEqual(response["pending_action"]["action_id"], current["action_id"])
            self.assertEqual(store.get_or_create(session)["patient_name"], corrected)

            incomplete = await handle_message(session, "لا، اسمي سارة", gateway, input_language="ar")
            self.assertEqual(incomplete["state"], "awaiting_patient_details")
            self.assertNotIn("pending_action", incomplete)
            self.assertEqual(store.get_or_create(session)["selected_slot_id"], "SL-1")
            self.assertEqual(store.get_or_create(session)["confirmed_phone"], "0599000111")
        gateway.call_write.assert_not_awaited()
