import json
from uuid import uuid4
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agent.slot_selection import format_slot_options
from app.main import app, get_gateway
from integrations import clinic_speech as speech
from test_voice_api import wav_bytes


class ClinicSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        speech.tickets.clear()
        self.addCleanup(speech.tickets.clear)
        env = patch.dict("os.environ", {"OPENROUTER_API_KEY": "synthetic-key",
                                        "OPENROUTER_BASE_URL": "https://speech.test/api/v1"})
        env.start()
        self.addCleanup(env.stop)
        app.dependency_overrides[get_gateway] = object
        self.addCleanup(app.dependency_overrides.pop, get_gateway)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://clinic.test")
        self.addAsyncCleanup(self.client.aclose)
        self.requests = []
        self.status = 200
        def fake_provider(request):
            self.requests.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://speech.test/api/v1/audio/speech")
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-key")
            return httpx.Response(self.status, content=b"\x01\x00" * 100,
                                  headers={"content-type": "audio/pcm"})
        create_client = httpx.AsyncClient
        self.create_client = create_client
        client_factory = patch.object(speech.httpx, "AsyncClient", side_effect=lambda **kwargs:
                                      create_client(transport=httpx.MockTransport(fake_provider), **kwargs))
        client_factory.start()
        self.addCleanup(client_factory.stop)

    async def test_typed_and_microphone_replies_use_the_same_gemini_speech_route(self):
        for language in ("ar", "en"):
            reply = "في أي يوم ترغب بحجز الموعد؟" if language == "ar" else "Which day would you like the appointment?"
            fake = AsyncMock(return_value={"reply": reply, "state": "awaiting_details", "language": language})
            with patch("app.main.handle_message", fake), patch("integrations.voice_adapter.transcribe", return_value=reply):
                chat = await self.client.post("/api/chat", json={"session_id": uuid4().hex, "message": reply})
                voice = await self.client.post("/api/voice", data={"session_id": uuid4().hex, "language_hint": language},
                                               files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})
            for response in (chat, voice):
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data["reply"], reply)
                self.assertEqual(data["speech_route"], "gemini")
                audio = await self.client.post(f"/api/speech/{data['speech_token']}")
                self.assertEqual(audio.status_code, 200)
                self.assertEqual(audio.headers["x-audio-sample-rate"], "24000")
                self.assertEqual(audio.content, b"\x01\x00" * 100)
                payload = self.requests[-1]
                self.assertEqual(payload["model"], "google/gemini-3.8-flash-lite-tts")
                self.assertEqual(payload["voice"], "Callirrhoe")
                self.assertEqual(payload["input"], reply)
                self.assertIn("Palestinian Arabic" if language == "ar" else "English", payload["instructions"])
            self.assertNotEqual(chat.json()["speech_token"], voice.json()["speech_token"])
        self.assertEqual(len(self.requests), 4)

    async def test_provider_failure_returns_retryable_status(self):
        self.status = 429
        data = speech.attach_speech({"language": "en", "reply": "Which day would you like the appointment?"}, {})
        response = await self.client.post(f"/api/speech/{data['speech_token']}")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Speech unavailable"})
        self.status = 200
        retry = await self.client.post(f"/api/speech/{data['speech_token']}")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])

    async def test_private_booking_stages_send_only_fixed_companions(self):
        private = "Synthetic Patient, 0599000111, BK-SAMPLE, 2026-11-15"
        for language in ("en", "ar"):
            for stage in ("awaiting_phone_confirmation", "awaiting_approval"):
                prefix = {
                    ("en", "awaiting_phone_confirmation"): "I heard your phone number as ",
                    ("ar", "awaiting_phone_confirmation"): "سمعت رقم تلفونك ",
                    ("en", "awaiting_approval"): "Please review and approve: ",
                    ("ar", "awaiting_approval"): "راجع التفاصيل ووافق لتأكيد الحجز: ",
                }[language, stage]
                reply = prefix + private
                fake = AsyncMock(return_value={"reply": reply, "state": stage, "language": language})
                with patch("app.main.handle_message", fake), patch("integrations.voice_adapter.transcribe", return_value="نعم" if language == "ar" else "yes"):
                    chat = await self.client.post("/api/chat", json={"session_id": uuid4().hex, "message": "test"})
                    voice = await self.client.post("/api/voice", data={"session_id": uuid4().hex, "language_hint": language},
                                                   files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})
                for response in (chat, voice):
                    data = response.json()
                    self.assertEqual(data["reply"], reply)
                    audio = await self.client.post(f"/api/speech/{data['speech_token']}")
                    self.assertEqual(audio.status_code, 200)
                    payload = self.requests[-1]
                    self.assertEqual(payload["voice"], "Callirrhoe")
                    for value in private.split(", "):
                        self.assertNotIn(value, json.dumps(payload))
                    self.assertIn("الشاشة" if language == "ar" else "screen", payload["input"])
                    self.assertEqual(payload["input"], speech.PRIVATE_COMPANIONS[stage][language])
        self.assertEqual(len(self.requests), 8)

    async def test_exact_synthetic_speech_requires_loopback_and_opt_in(self):
        for language, reply in (("en", "Review Synthetic Patient, 0599000111, BK-SAMPLE."),
                                ("ar", "راجع موعد سامر مثال ورقم 0599000111.")):
            fake = AsyncMock(return_value={"reply": reply, "state": "awaiting_approval", "language": language})
            for enabled, remote in ((False, False), (True, False), (True, True)):
                transport = httpx.ASGITransport(app=app, client=("192.0.2.10" if remote else "127.0.0.1", 12345))
                async with self.create_client(transport=transport, base_url="http://127.0.0.1:18001") as client:
                    with patch.dict("os.environ", {"ZAYTOUN_DEMO_EXACT_SPEECH": "synthetic-only" if enabled else ""}), \
                         patch("app.main.handle_message", fake), \
                         patch("integrations.voice_adapter.transcribe", return_value="نعم" if language == "ar" else "yes"):
                        for response in (
                            await client.post("/api/chat", json={"session_id": uuid4().hex, "message": "fixture"}),
                            await client.post("/api/voice", data={"session_id": uuid4().hex, "language_hint": language},
                                              files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")}),
                        ):
                            data = response.json()
                            self.assertEqual(data["reply"], reply)
                            audio = await self.client.post(f"/api/speech/{data['speech_token']}")
                            self.assertEqual(audio.status_code, 200)
                            payload = self.requests[-1]["input"]
                            if enabled and not remote:
                                self.assertEqual(payload, reply)
                            else:
                                self.assertNotIn("0599000111", payload)

    async def test_unclassified_and_private_replies_use_safe_companions(self):
        for stage, reply in (
            ("awaiting_details", "You previously requested Monday, which conflicts with Rania Sample."),
            ("slots_proposed", "1. 1:15 PM with Rania Sample 0599000111"),
        ):
            data = speech.attach_speech({"language": "en", "state": stage, "reply": reply}, {})
            self.assertNotIn("Rania Sample", speech.tickets[data["speech_token"]][1])
        data = speech.attach_speech({"language": "en", "state": "booking_confirmed",
                                     "reply": "Your appointment is confirmed. Booking ID: BK-SAMPLE",
                                     "result": {"ok": True}}, {})
        self.assertNotIn("confirmed", speech.tickets[data["speech_token"]][1])
        cancellation = speech.attach_speech({"language": "en", "state": "awaiting_approval",
                                             "reply": "Review and approve: Cancel booking BK-SAMPLE for Synthetic Patient.",
                                             "appointment_review": {"type": "cancel_booking"}}, {})
        self.assertIn("Review the cancellation", speech.tickets[cancellation["speech_token"]][1])
        self.assertEqual(self.requests, [])

    def test_slot_speech_uses_public_doctors_spoken_times_and_preserves_display(self):
        slot = {"date": "2026-11-16", "start": "13:15", "provider_id": "dr_khoury",
                "provider_name": "Dr. Rania Khoury", "provider_name_ar": "د. رانيا خوري"}
        for language, phrase in (("ar", "الساعة الواحدة والربع مساءً مع الدكتورة رانيا خوري"),
                                 ("en", "one fifteen p.m. with Doctor Rania Khoury")):
            reply = format_slot_options([slot], {"language": language})
            data = speech.attach_speech({"language": language, "state": "slots_proposed", "reply": reply},
                                        {"proposed_slots": [slot]})
            self.assertEqual(data["reply"], reply)
            text = speech.tickets[data["speech_token"]][1]
            self.assertIn(phrase, text)
            self.assertNotIn("1.", text)
            self.assertNotIn("1:15", text)
        alternate = "3:00 PM is unavailable. These nearby times are available:\n" + reply
        data = speech.attach_speech({"language": "en", "state": "slots_proposed", "reply": alternate},
                                    {"proposed_slots": [slot], "requested_time": "15:00"})
        self.assertIn("three p.m. is unavailable", speech.tickets[data["speech_token"]][1])
        data = speech.attach_speech({"language": "en", "state": "slots_proposed",
                                     "reply": "For patient Sample Name. " + reply},
                                    {"proposed_slots": [slot]})
        self.assertNotIn("Sample Name", speech.tickets[data["speech_token"]][1])

    async def test_verified_approval_outcomes_and_request_errors_are_spoken(self):
        from app.main import ApprovalRequest

        for language in ("en", "ar"):
            for status, stage in (("confirmed", "booking_confirmed"), ("cancelled", "booking_cancelled"),
                                  ("failed", "booking_confirmed")):
                body = ApprovalRequest(session_id=uuid4().hex, action_id="fixture", approved=True)
                result = {"ok": status != "failed", "booking_id": "BK-SAMPLE", "status": status}
                with patch("app.main.handle_approval", return_value={
                    "language": language, "state": stage, "reply": "Synthetic Patient BK-SAMPLE 0599000111",
                    "result": result,
                }):
                    response = await self.client.post("/api/approve", json=body.model_dump())
                audio = await self.client.post(f"/api/speech/{response.json()['speech_token']}")
                self.assertEqual(audio.status_code, 200)
                payload = self.requests[-1]["input"]
                for private in ("Synthetic Patient", "BK-SAMPLE", "0599000111"):
                    self.assertNotIn(private, payload)
                success = "تم تأكيد موعدك" if language == "ar" else "Your appointment is confirmed"
                self.assertEqual(success in payload, status == "confirmed")
            invalid = await self.client.post("/api/chat", json={"message": "BK-SAMPLE"},
                                             headers={"X-Clinic-Language": language})
            self.assertEqual(invalid.status_code, 422)
            await self.client.post(f"/api/speech/{invalid.json()['speech_token']}")
            self.assertNotIn("BK-SAMPLE", self.requests[-1]["input"])
            self.assertIn("الشاشة" if language == "ar" else "screen", self.requests[-1]["input"])


    def test_public_followup_and_private_review_have_distinct_speech_routes(self):
        for language, reply in (
            ("ar", "خيارات موعدك بعدها موجودة. أي خيار بناسبك؟"),
            ("en", "Your appointment options are still available. Which option would you prefer?"),
        ):
            data = speech.attach_speech({"language": language, "state": "slots_proposed", "reply": reply}, {})
            self.assertIn("speech_token", data)
        data = speech.attach_speech({"language": "en", "state": "awaiting_phone_confirmation",
                                     "reply": "Is 0599000111 correct?"}, {})
        self.assertEqual(data.get("speech_route"), "gemini")
        self.assertIn("speech_token", data)

    def test_generic_review_instructions_and_phone_retry_are_spoken(self):
        for language, stage, reply in (
            ("ar", "awaiting_approval", "للتأكيد اضغط زر الموافقة، أو الرفض إذا بدك موعد تاني."),
            ("en", "awaiting_approval", "Use Approve to confirm, or Reject to choose another appointment."),
            ("ar", "awaiting_patient_details", "احكي الأرقام رقم رقم أو اكتب الرقم."),
            ("en", "awaiting_patient_details", "Please say digits one by one or type the number."),
        ):
            with self.subTest(reply=reply):
                response = {"language": language, "state": stage, "reply": reply}
                if stage == "awaiting_approval":
                    response["appointment_review"] = {"patient_name": "Synthetic Patient"}
                data = speech.attach_speech(response, {})
                self.assertEqual(data["speech_route"], "gemini")
                self.assertEqual(speech.tickets[data["speech_token"]][1], reply)
