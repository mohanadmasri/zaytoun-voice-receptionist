# اختبارات الحالات المبلغ عنها عبر API وGraph الحقيقيين؛ جميع خدمات الصوت والعيادة وهمية.
# لا تثبت جودة ميكروفون أو سماعات حقيقية، ولا تنفذ أي كتابة حجز.
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from agent.dates import normalize_date
from agent.intake import IntakeResult, update_state_from_intake
from agent.service_mapping import explicit_service_evidence, match_service
from agent.slot_handoff import slot_matches_offer, slot_matches_request
from agent.scheduler import schedule
from agent.state import create_initial_state
from agent.times import normalize_time
from core.session_store import session_store
import test_arabic_conversation as conversation
from test_voice_api import wav_bytes
from test_voice_ui import app_ui
from voice.stt import _openrouter

SERVICES = conversation.SERVICES


# تختبر إعداد طلب STT فقط: لا صوت حقيقي ولا اتصال خارجي أو مفاتيح حقيقية.
class TranscriptionContractTests(unittest.TestCase):
    def test_explicit_language_prompt_and_arabic_fallback(self):
        for language, transcript in (("ar", "بدي فحص أسنان"), ("en", "I need a dental checkup")):
            response = httpx.Response(200, json={"model": "fake", "choices": [{"message": {"content": transcript}}]})
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-key", "STT_MODEL": "google/gemini-2.5-flash-lite",
                                         "STT_FALLBACK_MODEL": "mistralai/voxtral-small-24b-2507"}), \
                 patch("voice.stt.httpx.post", return_value=response) as post:
                self.assertEqual(_openrouter(wav_bytes(), "wav", language), transcript)
            body = post.call_args.kwargs["json"]
            prompt = body["messages"][0]["content"][0]["text"]
            self.assertIn("Never translate", prompt)
            self.assertIn("Arabic" if language == "ar" else "English", prompt)
            self.assertIn("selected by the caller", prompt)
            if language == "ar":
                self.assertNotIn("models", body)
            else:
                self.assertIn("mistralai/voxtral-small-24b-2507", body["models"])

    def test_spoken_dates_hours_and_service_evidence(self):
        for text in ("November second, 2026", "second November 2026", "2 نوفمبر 2026", "٢ نوفمبر ٢٠٢٦", "يوم الاثنين 2 نوفمبر"):
            self.assertEqual(normalize_date(text, "2026-11-02"), "2026-11-02", text)
        self.assertEqual(normalize_date("November twenty-third, 2026", "2026-11-02"), "2026-11-23")
        result = IntakeResult(date_text="Tuesday at three o'clock")
        self.assertEqual(result.date_text, "Tuesday")
        self.assertEqual(normalize_time(result.time_text)["kind"], "ambiguous")
        self.assertEqual(normalize_time("three o'clock in the afternoon")["requested_time"], "15:00")
        self.assertEqual(normalize_time("after three PM")["after"], "15:01")
        for text, expected in (("I need a dental checkup.", "dental_checkup"), ("check up", "dental_checkup"),
                               ("dental cleaning", "dental_cleaning"), ("dental filling", "dental_filling"),
                               ("فحص الاسنان", "dental_checkup"), ("فحص أسنان", "dental_checkup")):
            self.assertEqual(match_service(explicit_service_evidence(text), SERVICES), expected, text)
        self.assertIsNone(explicit_service_evidence("I need a dentist"))
        self.assertIsNone(explicit_service_evidence("بدي موعد دكتور اسنان"))
        self.assertIsNone(explicit_service_evidence("I need a heart checkup"))

    def test_ui_forwards_language_and_continuation(self):
        from types import SimpleNamespace
        response = {"reply": "Hello", "state": "awaiting_details", "tool_calls": []}
        with patch("ui.app_ui.post", return_value=response) as post:
            app_ui.send_voice("samples/audio/en_02_vague_friday.wav", [{"role": "user", "content": "Hi"}],
                              SimpleNamespace(session_hash="language-test"), "English")
        self.assertEqual(post.call_args.kwargs["data"]["language_hint"], "en")
        self.assertEqual(post.call_args.kwargs["data"]["continuation"], "true")
        with self.assertRaises(Exception):
            app_ui.session_id(SimpleNamespace(session_hash=None))


# تستعير أدوات الاختبار فقط؛ تحميل حالات الأب يتم في مجموعة الانحدار الأصلية.
class ComprehensiveConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = conversation.ArabicConversationTests.asyncSetUp
    asyncTearDown = conversation.ArabicConversationTests.asyncTearDown
    send = conversation.ArabicConversationTests.send
    state = conversation.ArabicConversationTests.state
    search = conversation.ArabicConversationTests.search

    async def test_explicit_pm_followups_preserve_service_and_date(self):
        for voice in (False, True):
            for message, extracted in (("Do you have anything around 2 PM?", "2"),
                                       ("Two p m", "Two p m"),
                                       ("I want an appointment at 2 PM", "at 2")):
                with self.subTest(voice=voice, message=message):
                    session_store.delete(self.session_id)
                    self.gateway.reads.clear()
                    ambiguous = await self.send(
                        "Dental checkup on November 2, 2026 at 2 o'clock", language="en",
                        service_text="dental checkup", date_text="November 2, 2026",
                        time_text="at 2 o'clock", provider_any=True,
                    )
                    self.assertIn("morning or afternoon", ambiguous["reply"])
                    self.assertFalse(any(name == "find_slots" for name, _ in self.gateway.reads))
                    if voice:
                        async def extract(_message):
                            return IntakeResult(language="en", intent="booking", time_text=extracted)

                        with patch("agent.graph.extract_request", new=extract), \
                             patch("integrations.voice_adapter.transcribe", return_value=message), \
                             patch("integrations.voice_adapter.speak", return_value=None):
                            response = await self.client.post(
                                "/api/voice", data={"session_id": self.session_id, "language_hint": "en"},
                                files={"audio": ("test.wav", wav_bytes() + message.encode(), "audio/wav")},
                            )
                        self.assertEqual(response.status_code, 200, response.text)
                        data = response.json()
                        self.assertEqual(data["transcript"], message)
                    else:
                        data = await self.send(message, language="en", time_text=extracted)

                    self.assertEqual(data["state"], "slots_proposed", data["reply"])
                    state = self.state()
                    self.assertEqual(state["service_id"], "dental_checkup")
                    self.assertEqual(state["requested_date"], "2026-11-02")
                    self.assertEqual(state["requested_time"], "14:00")
                    self.assertIn("nearby", data["reply"].lower())
                    self.assertTrue(state["proposed_slots"])
                    for slot in state["proposed_slots"]:
                        self.assertEqual(slot["date"], "2026-11-02")
                        self.assertEqual(slot["service_id"], "dental_checkup")
                        self.assertTrue("12:00" <= slot["start"] <= "16:00", slot)
                    self.assertIsNone(data.get("pending_action"))

    async def test_observed_wrong_llm_service_recovers_without_losing_time(self):
        data = await self.send("I need a dentist on November 2, 2026 after 3 PM", language="en",
                               service_text="dentist appointment", date_text="November 2, 2026", time_text="after 3 PM", provider_any=True)
        self.assertEqual(data["state"], "awaiting_details")
        data = await self.send("dental checkup", language="en", service_text="dentist appointment")
        self.assertEqual(data["state"], "slots_proposed")
        self.assertEqual(self.state()["service_id"], "dental_checkup")
        self.assertEqual(self.state()["time_after"], "15:01")
        self.assertTrue(all(slot["start"] > "15:00" for slot in self.state()["proposed_slots"]))
        data = await self.send("check up", language="en")
        self.assertIn(data["state"], ("slots_proposed", "awaiting_slot_selection"))
        self.assertNotIn("Would you like Dental", data["reply"])
        self.assertEqual(self.state()["time_after"], "15:01")

    async def test_nearby_search_preserves_specific_doctor_and_after_constraint(self):
        state = create_initial_state(self.session_id)
        state.update({"stage": "needs_slot_lookup", "language": "en", "service_id": "dental_checkup",
                      "requested_date": "2026-11-02", "time_kind": "exact", "requested_time": "14:00",
                      "provider_choice": "specific", "provider_id": "dr_nassar"})
        result = await schedule(state, self.gateway)
        self.assertEqual(result["stage"], "slots_proposed")
        self.assertTrue(all(slot["provider_id"] == "dr_nassar" for slot in result["proposed_slots"]))
        self.assertTrue(all(args.get("provider_id") == "dr_nassar" for name, args in self.gateway.reads if name == "find_slots"))
        self.gateway.reads.clear()
        state.update({"time_kind": "window", "requested_time": None, "time_after": "19:01"})
        result = await schedule(state, self.gateway)
        self.assertEqual(result["stage"], "no_slots")
        self.assertEqual(len(self.gateway.reads), 1)
        self.assertEqual(self.gateway.reads[0][1]["after"], "19:01")

    async def test_explicit_service_missing_date_and_optional_time(self):
        data = await self.send("I need a dental checkup.", language="en", service_text="dentist appointment")
        self.assertEqual(data["state"], "awaiting_details")
        self.assertIn("day", data["reply"].lower())
        data = await self.send("November second, 2026", language="en", date_text="November second, 2026")
        self.assertEqual(data["state"], "slots_proposed")
        self.assertEqual(self.state()["time_kind"], "missing")
        self.assertTrue(any(slot["start"] == "09:00" for slot in self.state()["proposed_slots"]))

    async def test_invented_provider_is_not_accepted_from_transcript(self):
        data = await self.send("I need a dental checkup on November 2, 2026 with Dr. Aslan", language="en",
                               service_text="dental checkup", date_text="November 2, 2026", provider_text="Dr. Aslan")
        self.assertEqual(data["state"], "awaiting_provider_clarification")
        self.assertIsNone(self.state()["provider_id"])
        self.assertIsNone(self.state()["pending_action"])
        self.assertFalse(any(name == "find_slots" for name, _ in self.gateway.reads))

    async def test_wrong_script_never_reaches_graph_or_shows_invented_doctor(self):
        await self.search()
        await self.send("الخيار الثاني")
        await self.send("اسمي مريضة اختبار ورقمي 0599000999", patient_name="مريضة اختبار", patient_phone="0599000999")
        before = deepcopy(self.state())
        for language, transcript in (("ar", "I have a dental checkup with Dr. Aslan"), ("en", "بدي موعد فحص أسنان")):
            with patch("integrations.voice_adapter.transcribe", return_value=transcript) as stt, \
                 patch("agent.graph.extract_request") as extract:
                response = await self.client.post("/api/voice", data={"preview_only": "true", "session_id": self.session_id, "language_hint": language},
                                                  files={"audio": ("test.wav", wav_bytes(), "audio/wav")})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["transcript"], "")
            self.assertNotIn("Aslan", response.json()["reply"])
            self.assertEqual(response.json()["pending_action"], before["pending_action"])
            extract.assert_not_called()
            self.assertEqual(stt.call_args.args[1], language)
            self.assertEqual(self.state(), before)

    async def test_voice_language_switch_keeps_selection_and_patient(self):
        await self.search()
        for language, transcript, expected in (("en", "option two", "awaiting_patient_details"),
                                                ("ar", "اسمي مريضة اختبار ورقمي 0599000999", "awaiting_approval")):
            result = IntakeResult(language="unknown", intent="booking", **(
                {"patient_name": "مريضة اختبار", "patient_phone": "0599000999"} if language == "ar" else {}))
            async def fake_extract(_message):
                return result
            with patch("integrations.voice_adapter.transcribe", return_value=transcript), \
                 patch("integrations.voice_adapter.speak", return_value=None), patch("agent.graph.extract_request", new=fake_extract):
                response = await self.client.post("/api/voice", data={"preview_only": "true", "session_id": self.session_id, "language_hint": language, "continuation": "true"},
                                                  files={"audio": ("test.wav", wav_bytes() + language.encode(), "audio/wav")})
                response = await self.client.post("/api/voice/confirm", json={"session_id": self.session_id, "transcript_token": response.json()["transcript_token"]})
            self.assertEqual(response.status_code, 200, response.text)
            from test_support import confirm_phone_turn
            response = await confirm_phone_turn(self.client, response, self.session_id)
            self.assertEqual(response.json()["language"], language)
            self.assertEqual(response.json()["state"], expected)
            self.assertFalse(response.json()["session_reset"])
            self.assertEqual(response.json()["transcript"], transcript)
        self.assertEqual(self.state()["selected_slot_id"], "SL-CONV-3")

    async def test_continuation_reports_missing_session_only(self):
        async def fake_extract(_message):
            return IntakeResult(language="en")
        with patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/chat", json={"session_id": self.session_id, "message": "Hello", "continuation": True})
            self.assertTrue(response.json()["session_reset"])
            self.assertIn("previous session", response.json()["reply"])
            response = await self.client.post("/api/chat", json={"session_id": self.session_id, "message": "Hello", "continuation": True})
            self.assertFalse(response.json()["session_reset"])

    async def test_exact_alternatives_current_offer_only_and_prepare_approval(self):
        original = self.gateway.call_read
        async def no_exact(name, args):
            result = await original(name, args)
            if name == "find_slots":
                result["slots"] = [slot for slot in result["slots"] if slot["start"] != "15:00"]
                result["slots"] += [{**result["slots"][-1], "slot_id": "WRONG-DATE", "date": "2026-11-03"},
                                     {**result["slots"][-1], "slot_id": "WRONG-SERVICE", "service_id": "dental_filling"}]
            return result
        self.gateway.call_read = no_exact
        data = await self.send("بدي فحص أسنان يوم الاثنين 2 نوفمبر 2026 الساعة 3 العصر عند أي دكتور متاح",
                               service_text="دكتور اسنان", date_text="2 نوفمبر 2026", time_text="الساعة 3 العصر", provider_any=True)
        self.assertEqual(data["state"], "slots_proposed")
        self.assertIn("مش متاحة", data["reply"])
        state = self.state()
        self.assertEqual(state["requested_time"], "15:00")
        self.assertIsNone(state["selected_slot_id"])
        for slot in state["proposed_slots"]:
            self.assertEqual(slot["date"], "2026-11-02")
            self.assertEqual(slot["service_id"], "dental_checkup")
            self.assertGreaterEqual(slot["start"], "13:00")
            self.assertFalse(slot_matches_request(state, slot))
            self.assertTrue(slot_matches_offer(state, slot))
            self.assertFalse(slot_matches_offer({**state, "offer_context": None}, slot))
            self.assertFalse(slot_matches_offer({**state, "requested_time": slot["start"]}, slot))
        selected = state["proposed_slots"][1]["slot_id"]
        await self.send("الخيار الثاني")
        data = await self.send("اسمي مريضة اختبار ورقمي 0599000999", patient_name="مريضة اختبار", patient_phone="0599000999")
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(data["pending_action"]["payload"]["slot_id"], selected)
        before = self.state()
        state = update_state_from_intake({**before, "current_message": "dental checkup"}, IntakeResult(language="en", service_text="dentist appointment"))
        self.assertEqual(state["pending_action"], before["pending_action"])
        self.assertEqual(state["selected_slot_id"], selected)
        data = await self.send("نعم")
        self.assertEqual(data["state"], "awaiting_approval")


if __name__ == "__main__":
    unittest.main(verbosity=2)
