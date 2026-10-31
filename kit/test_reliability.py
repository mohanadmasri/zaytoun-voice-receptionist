# اختبارات السلامة والذاكرة ومراجعة الصوت؛ كل LLM وSTT وكتابات العيادة بدائل محلية.
# WAV إشارة اختبار لا كلام حقيقي؛ لا تثبت هذه الاختبارات جودة الميكروفون أو النطق المسموع.
import asyncio
import io
import unittest
import wave
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from app.main import app, get_voice_adapter
from agent.dates import normalize_date
from agent.intake import IntakeResult, update_state_from_intake
from agent.patient_identity import valid_patient_name
from agent.slot_selection import select_slot
from actions.approval import prepare_booking_action, handle_approval
from core.session_store import session_store
from integrations.voice_adapter import validate_audio, VoiceAdapter, VoiceError
from test_voice_api import wav_bytes
from agent.scheduler import schedule
from agent.state import create_initial_state
from agent.graph import patient_lookup_node
import test_arabic_conversation as conversation


# تحقق محلي من الأسماء والتواريخ ومستوى الإشارة دون خدمات خارجية.
class ReliabilityNormalizationTests(unittest.TestCase):
    def test_names_support_arabic_and_reject_relationships(self):
        for name in ("44", "my son", "my daughter", "ابني", "", "unknown", "Test Patient", "0599000999", "Lina", "Lina 44"):
            self.assertFalse(valid_patient_name(name), name)
        for name in ("ليان أحمد", "محمد عبد الرحمن", "Lina Mansour", "Anne-Marie O'Neil"):
            self.assertTrue(valid_patient_name(name), name)

    def test_spoken_calendar_dates_and_weekday_conflicts(self):
        for text, expected in (("يوم الاثنين الثاني من نوفمبر", "2026-11-02"), ("يوم الثلاثاء 3 من نوفمبر", "2026-11-03"),
                               ("November 3rd", "2026-11-03"), ("November third", "2026-11-03")):
            self.assertEqual(normalize_date(text, "2026-10-31"), expected, text)
        self.assertIsNone(normalize_date("يوم الخميس 3 من نوفمبر", "2026-10-31"))

    def test_silence_clipping_rejected_without_minimum_word_count(self):
        for sample in (b"\0\0", b"\xff\x7f"):
            out = io.BytesIO()
            with wave.open(out, "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(sample * 1600)
            with self.assertRaises(VoiceError):
                validate_audio(out.getvalue())
        validate_audio(wav_bytes())

    def test_stereo_phase_cancellation_is_not_sent_as_speech(self):
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\xe8\x03\x18\xfc" * 1600)
        with self.assertRaises(VoiceError):
            validate_audio(out.getvalue())


# تتحقق من قبول العبارات القصيرة ومن ميزانية إعادة المحاولة، دون كشف ثقة مختلقة.
class ShortSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_speech_is_not_rejected_for_length_or_words(self):
        for text, language in (("3 ونص", "ar"), ("three thirty", "en"), ("yes", "en"), ("option two", "en"), ("3:30", "ar")):
            with patch("integrations.voice_adapter.transcribe", return_value=text):
                self.assertEqual(await VoiceAdapter().transcribe(wav_bytes(), language), text)


# تستعمل Graph وHTTP الحقيقيين وبوابة قراءة تمنع الكتابة؛ لا تعتمد على نتائج مزود متغيرة.
class ReliabilityConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = conversation.ArabicConversationTests.asyncSetUp
    send = conversation.ArabicConversationTests.send
    search = conversation.ArabicConversationTests.search
    state = conversation.ArabicConversationTests.state

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_voice_adapter, None)
        for store in (app.state.voice_reviews, app.state.voice_generations, app.state.last_audio):
            store.pop(self.session_id, None)
        await conversation.ArabicConversationTests.asyncTearDown(self)

    # تجهز موافقة محلية فقط لا ترسل قرار الموافقة للعيادة.
    async def pending(self):
        await self.search()
        await self.send("الخيار الثاني")
        await self.send("اسمي ليان أحمد ورقمي 0599000999", patient_name="ليان أحمد", patient_phone="0599000999")
        self.assertEqual(self.state()["stage"], "awaiting_approval")

    # ترفع ملفًا إلى مرحلة المراجعة فقط؛ لا يمر النص إلى Graph قبل confirm.
    async def preview(self, text, audio=None, language="ar"):
        with patch("integrations.voice_adapter.transcribe", return_value=text):
            return await self.client.post("/api/voice", data={"session_id": self.session_id, "language_hint": language},
                                          files={"audio": ("speech.wav", audio or wav_bytes(), "audio/wav")})

    async def confirm(self, token):
        return await self.client.post("/api/voice/confirm", json={"session_id": self.session_id, "transcript_token": token})

    async def test_unrelated_same_language_transcript_cannot_mutate_pending(self):
        await self.pending()
        before = self.state()
        with patch("agent.graph.extract_request") as extract:
            response = await self.preview("مرحبا بدي أحجز موعد لابني")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["needs_transcript_confirmation"])
        self.assertEqual(self.state(), before)
        self.assertEqual(response.json()["pending_action"], before["pending_action"])
        extract.assert_not_called()

    async def test_replayed_recording_and_stale_preview_rejected(self):
        await self.pending()
        before = self.state()
        first = await self.preview("3 ونص")
        token = first.json()["transcript_token"]
        repeated = await self.preview("مرحبا بدي موعد لابني")
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual((await self.confirm(token)).status_code, 409)
        self.assertEqual(self.state(), before)

    async def test_text_turn_invalidates_preview(self):
        await self.search()
        first = await self.preview("الخيار الثاني")
        await self.send("الخيار الثالث")
        chosen = self.state()["selected_slot_id"]
        self.assertEqual((await self.confirm(first.json()["transcript_token"])).status_code, 409)
        self.assertEqual(self.state()["selected_slot_id"], chosen)

    async def test_short_utterance_confirmed_once_without_llm(self):
        await self.search()
        first = await self.preview("3 ونص")
        token = first.json()["transcript_token"]
        with patch("agent.graph.extract_request") as extract:
            response = await self.confirm(token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.state()["selected_slot_id"], "SL-CONV-2")
        extract.assert_not_called()
        self.assertEqual((await self.confirm(token)).status_code, 409)

    async def test_out_of_order_recording_response_cannot_replace_latest(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowAdapter:
            async def transcribe(self, data, hint):
                if data.endswith(b"OLD"):
                    entered.set()
                    await release.wait()
                    return "الخيار الأول"
                return "الخيار الثاني"
        app.dependency_overrides[get_voice_adapter] = lambda: SlowAdapter()
        async def upload(suffix):
            return await self.client.post("/api/voice", data={"session_id": self.session_id, "language_hint": "ar"},
                                          files={"audio": ("speech.wav", wav_bytes() + suffix, "audio/wav")})
        old = asyncio.create_task(upload(b"OLD"))
        await asyncio.wait_for(entered.wait(), 2)
        current = await upload(b"NEW")
        release.set()
        self.assertEqual((await old).status_code, 409)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(app.state.voice_reviews[self.session_id]["text"], "الخيار الثاني")

    async def test_confirm_timeout_preserves_identity_selection_and_pending(self):
        await self.pending()
        before = self.state()
        response = await self.preview("نعم")
        with patch("app.main.process_message", side_effect=TimeoutError):
            response = await self.confirm(response.json()["transcript_token"])
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.state(), before)
        self.assertEqual(response.json()["pending_action"], before["pending_action"])

    async def test_patient_switch_does_not_inherit_old_phone(self):
        await self.pending()
        old = self.state()
        updated = update_state_from_intake({**old, "current_message": "I want an appointment for my son"},
                                          IntakeResult(language="en", intent="booking", patient_name="my son"))
        for field in ("patient_name", "patient_phone", "selected_slot_id", "pending_action"):
            self.assertIsNone(updated[field], field)
        self.assertTrue(updated["patient_age_unverified"])
        updated = update_state_from_intake({**old, "current_message": "اسمي سلمى محمود"},
                                          IntakeResult(language="ar", patient_name="سلمى محمود"))
        self.assertIsNone(updated["patient_phone"])
        self.assertIsNone(updated["pending_action"])

    async def test_invalid_identity_cannot_prepare_or_execute_old_approval(self):
        await self.pending()
        baseline = self.state()
        for name in ("44", "my son"):
            state = deepcopy(baseline)
            state["patient_name"] = name
            with self.assertRaises(ValueError):
                prepare_booking_action(state)
            state["pending_action"]["payload"]["patient_name"] = name
            session_store.save(state)
            gateway = type("WriteMock", (), {"call_write": AsyncMock()})()
            response = await handle_approval(self.session_id, state["pending_action"]["action_id"], True, gateway)
            gateway.call_write.assert_not_awaited()
            self.assertEqual(response["state"], "awaiting_patient_details")

    async def test_minor_guardian_policy_is_not_bypassed(self):
        await self.pending()
        state = update_state_from_intake({**self.state(), "current_message": "for my son aged 7"}, IntakeResult(language="en"))
        self.assertTrue(state["requires_guardian"])
        state.update({"patient_name": "Adam Odeh", "patient_phone": "0599000333", "selected_slot_id": "SL-CONV-3"})
        with self.assertRaises(ValueError):
            prepare_booking_action(state)

    async def test_completed_and_rejected_lifecycle_does_not_reuse_details(self):
        for stage in ("booking_confirmed", "booking_rejected"):
            await self.pending()
            state = self.state()
            state.update({"stage": stage, "pending_action": None})
            session_store.save(state)
            self.gateway.reads.clear()
            data = await self.send("مرحبا")
            for field in ("patient_name", "patient_phone", "service_id", "requested_date", "selected_slot_id", "pending_action"):
                self.assertIsNone(self.state()[field], field)
            self.assertEqual(self.state()["proposed_slots"], [])
            self.assertEqual(self.gateway.reads, [])

    async def test_greeting_preserves_pending_booking(self):
        await self.pending()
        before = self.state()
        data = await self.send("مرحبا")
        self.assertEqual(data["pending_action"]["action_id"], before["pending_action"]["action_id"])
        self.assertEqual(self.state()["patient_phone"], before["patient_phone"])

    async def test_actual_approval_lifecycle_with_local_mock_only(self):
        await self.pending()
        state = self.state()
        gateway = type("WriteMock", (), {"call_write": AsyncMock(return_value={"ok": True, "booking_id": "MOCK-ONLY"})})()
        action_id = state["pending_action"]["action_id"]
        result = await handle_approval(self.session_id, action_id, True, gateway)
        self.assertEqual(result["state"], "booking_confirmed")
        self.assertEqual(self.state()["proposed_slots"], [])
        self.assertIsNone(self.state()["selected_slot_id"])
        await handle_approval(self.session_id, action_id, True, gateway)
        gateway.call_write.assert_awaited_once()
        await self.send("مرحبا")
        self.assertIsNone(self.state()["patient_phone"])
        await self.pending()
        state = self.state()
        await handle_approval(self.session_id, state["pending_action"]["action_id"], False, gateway)
        self.assertEqual(self.state()["stage"], "booking_rejected")
        self.assertEqual(self.state()["proposed_slots"], [])
        gateway.call_write.assert_awaited_once()

    async def test_other_patient_phone_lookup_does_not_become_patient_name(self):
        from types import SimpleNamespace
        state = create_initial_state(self.session_id)
        state.update({"patient_phone": "0599000333", "patient_is_other": True})
        updates = await patient_lookup_node(state, SimpleNamespace(context={"gateway": self.gateway}))
        self.assertNotIn("patient_name", updates)

    async def test_nearby_order_is_stable_for_reversed_tool_output(self):
        state = create_initial_state(self.session_id)
        state.update({"stage": "needs_slot_lookup", "service_id": "dental_checkup", "requested_date": "2026-11-02",
                      "provider_choice": "any", "time_kind": "exact", "requested_time": "14:30"})
        first = await schedule(state, self.gateway)
        original = self.gateway.call_read
        async def reversed_read(name, args):
            result = await original(name, args)
            if name == "find_slots":
                result["slots"].reverse()
            return result
        self.gateway.call_read = reversed_read
        second = await schedule(state, self.gateway)
        self.assertEqual(first["proposed_slots"], second["proposed_slots"])
        self.assertIsNone(second["selected_slot_id"])

    async def test_arabic_clarification_preserves_spoken_date_and_morning_time(self):
        await self.send("بدي موعد دكتور أسنان يوم الثلاثاء 3 من نوفمبر الساعة 5 صباحا", service_text="دكتور أسنان",
                        date_text="يوم الثلاثاء 3 من نوفمبر", time_text="الساعة 5 صباحا")
        self.assertEqual(self.state()["requested_date"], "2026-11-03")
        self.assertEqual(self.state()["requested_time"], "05:00")
        data = await self.send("فحص أسنان دوري", service_text="دكتور أسنان")
        self.assertEqual(self.state()["requested_date"], "2026-11-03")
        self.assertEqual(self.state()["requested_time"], "05:00")
        self.assertEqual(data["state"], "no_slots")

    async def test_conflicting_weekday_keeps_original_until_explicit_date(self):
        await self.send("I want a dental checkup on November 3rd after 5 PM", language="en", service_text="dental checkup",
                        date_text="November 3rd", time_text="after 5 PM")
        data = await self.send("Thursday.", language="en", date_text="Thursday")
        self.assertIn("conflicts", data["reply"])
        self.assertEqual(self.state()["requested_date"], "2026-11-03")
        self.assertEqual(self.state()["time_after"], "17:01")
        await self.send("November 5, 2026", language="en", date_text="November 5, 2026")
        self.assertEqual(self.state()["requested_date"], "2026-11-05")
        self.assertIsNone(self.state().get("date_conflict"))

    async def test_natural_selection_forms_against_current_options(self):
        await self.search()
        state = self.state()
        for phrase in ("option three", "I want the third option", "option number 3", "الخيار الثالث", "الموعد الثالث"):
            self.assertEqual(select_slot(state, phrase)["selected_slot_id"], state["proposed_slots"][2]["slot_id"], phrase)
        for phrase in ("three thirty", "three and a half", "الساعة ثلاثة ونص", "3 ونص"):
            self.assertEqual(select_slot(state, phrase)["selected_slot_id"], "SL-CONV-2", phrase)
        for phrase in ("I prefer Dr. Sami", "بدي الدكتور سامي", "I prefer the option with Sami Nasser"):
            self.assertEqual(select_slot(state, phrase)["stage"], "awaiting_slot_selection", phrase)
        self.assertEqual(select_slot(state, "3:45 with Dr. Sami")["selected_slot_id"], "SL-CONV-4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
