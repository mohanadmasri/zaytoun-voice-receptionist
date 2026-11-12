import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from integrations.clinic_speech import tickets
from agent.intake import IntakeResult
from agent.state import create_initial_state
from app.main import app, get_gateway
from core.session_store import session_store
from test_voice_api import wav_bytes

DATA = Path(__file__).parent / "services/data"
PROVIDERS = json.loads((DATA / "providers.json").read_text(encoding="utf-8"))
SERVICES = json.loads((DATA / "services.json").read_text(encoding="utf-8"))

FIRST = "بدي أحجز موعد أسنان بكرة على الساعة ثلاثة"
FILLING = "حشوة أسنان"
HOURS_REPLY = "هالساعة خارج دوام العيادة. اختار ساعة تانية، لو سمحت."


class LiveExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = uuid4().hex
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        app.dependency_overrides[get_gateway] = lambda: self
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.pop, get_gateway)
        self.addCleanup(session_store.delete, self.session)
        self.addCleanup(app.state.session_locks.pop, self.session, None)
        self.friday_close = "13:00"
        self.friday_start = "11:00"
        self.friday_extra = []
        self.range_slots = None
        self.today = "2026-11-12"
        self.reads = []

    async def call_read(self, tool, args):
        if tool == "clinic_info":
            return {"ok": True, "today": self.today, "now": self.today + "T10:00:00+02:00",
                    "providers": PROVIDERS, "opening_hours": {
                        "thursday": {"open": "09:00", "close": "19:00"},
                        "friday": {"open": "09:00", "close": self.friday_close},
                        "saturday": {"open": "10:00", "close": "16:00"}, "sunday": None,
                        "monday": {"open": "09:00", "close": "19:00"},
                    }}
        if tool == "list_services":
            return {"ok": True, "services": SERVICES}
        if tool == "get_patient":
            return {"ok": True, "patient": None}
        if tool == "find_slots":
            self.reads.append(args)
            if self.range_slots is not None and args["date_from"] != args["date_to"]:
                return {"ok": True, "slots": [
                    {"slot_id": f"range-{day}-{clock}", "date": day, "start": clock,
                     "service_id": args["service_id"], "provider_id": "dr_khoury",
                     "provider_name": "Dr. Rania Khoury", "provider_name_ar": "د. رانيا خوري"}
                    for day, clock in self.range_slots
                ]}
            day = args["date_from"]
            start = self.friday_start if day == "2026-11-13" else "14:00"
            starts = [start, *self.friday_extra] if day == "2026-11-13" else [start]
            slots = [{"slot_id": f"fixture-{day}-{clock}", "date": day, "start": clock,
                      "service_id": args["service_id"], "provider_id": "dr_khoury",
                      "provider_name": "Dr. Rania Khoury", "provider_name_ar": "د. رانيا خوري"}
                     for clock in starts if clock]
            return {"ok": True, "slots": slots if day != "2026-11-15" else []}
        self.fail(f"Unexpected read {tool}")

    async def call_write(self, tool, args):
        self.fail(f"Unexpected write {tool}")

    async def send(self, message, language="ar", **fields):
        with patch("agent.graph.extract_request", return_value=IntakeResult(language=language, intent="booking", **fields)):
            result = await self.client.post("/api/chat", json={"session_id": self.session, "message": message})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def state(self):
        return session_store.get_or_create(self.session)

    async def test_exact_live_hours_reply_uses_gemini(self):
        self.friday_start = None
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "synthetic-key"}):
            first = await self.send(FIRST, service_text="أسنان", date_text="بكرة", time_text="الساعة ثلاثة")
            self.assertEqual(first["speech_route"], "gemini")
            self.addCleanup(tickets.pop, first["speech_token"], None)
            reply = await self.send(FILLING, service_text=FILLING)
        self.assertEqual(reply["reply"], HOURS_REPLY)
        self.assertEqual(reply["speech_route"], "gemini", reply)
        self.addCleanup(tickets.pop, reply["speech_token"], None)

    async def test_relative_date_and_clock_survive_service_clarification(self):
        first = await self.send(FIRST, service_text="أسنان")
        self.assertEqual(first["state"], "awaiting_details")
        self.assertIn("حشوة", first["reply"])
        self.assertEqual(self.state()["requested_date"], "2026-11-13")
        self.assertEqual(self.state()["requested_time"], "15:00")
        second = await self.send(FILLING, service_text=FILLING)
        self.assertEqual(self.state()["requested_date"], "2026-11-13")
        self.assertEqual(self.state()["requested_time"], "15:00")
        self.assertEqual(self.state()["service_id"], "dental_filling")
        self.assertEqual(second["state"], "slots_proposed")
        self.assertTrue(self.state()["proposed_slots"])
        self.assertTrue(all(slot["start"] == "11:00" for slot in self.state()["proposed_slots"]))
        self.assertIsNone(second.get("pending_action"))

    async def test_relative_days_and_ambiguous_week(self):
        for phrase, expected in (
            ("بكرا", "2026-11-13"), ("بعد بكرا", "2026-11-14"), ("بعد يومين", "2026-11-14"),
            ("بعد ثلاثة أيام", "2026-11-15"), ("اليوم", "2026-11-12"),
            ("الأحد الجاي", "2026-11-15"), ("الاثنين الجاي", "2026-11-16"), ("الخميس الجاي", "2026-11-19"),
            ("tomorrow", "2026-11-13"), ("the day after tomorrow", "2026-11-14"),
            ("next Monday", "2026-11-16"), ("الأسبوع الجاي", None),
        ):
            with self.subTest(phrase=phrase):
                await self.send(phrase, language="en" if phrase.isascii() else "ar")
                self.assertEqual(self.state()["requested_date"], expected)

    async def test_open_day_conflict_offers_verified_nearby_time(self):
        self.friday_close = "19:00"
        self.friday_start = "14:30"
        await self.send(FIRST, service_text="أسنان", time_text="03:00")
        second = await self.send(FILLING, service_text=FILLING)
        self.assertEqual(self.state()["requested_time"], "15:00")
        self.assertEqual(self.reads[0]["after"], "15:00")
        self.assertEqual(second["state"], "slots_proposed")
        self.assertIn("مش متاحة", second["reply"])
        self.assertEqual([s["start"] for s in self.state()["proposed_slots"]], ["14:30"])
        self.assertIsNone(second.get("pending_action"))

    async def test_closed_day_keeps_original_date_and_offers_next_open_day(self):
        result = await self.send("بدي حشوة أسنان الأحد الجاي على الساعة ثلاثة", service_text=FILLING)
        self.assertEqual(self.state()["requested_date"], "2026-11-15")
        self.assertEqual(self.state()["requested_time"], "15:00")
        self.assertEqual(result["state"], "slots_proposed")
        self.assertIn("مسكّرة", result["reply"])
        self.assertEqual([s["date"] for s in self.state()["proposed_slots"]], ["2026-11-16"])
        self.assertIsNone(result.get("pending_action"))

    async def test_next_week_overrides_guessed_day_and_asks_for_exact_day(self):
        result = await self.send("بدي حشوة أسنان الأسبوع الجاي", service_text=FILLING, date_text="next Monday")
        self.assertIsNone(self.state()["requested_date"])
        self.assertEqual(result["reply"], "في أي يوم ترغب بحجز الموعد؟")
        self.assertEqual(self.reads, [])
        self.assertIsNone(result.get("pending_action"))

    async def test_clarification_after_midnight_keeps_original_tomorrow(self):
        await self.send(FIRST, service_text="أسنان")
        self.today = "2026-11-13"
        await self.send(FILLING, service_text=FILLING)
        self.assertEqual(self.state()["requested_date"], "2026-11-13")
        self.assertEqual(self.state()["requested_time"], "15:00")

    async def test_elapsed_time_today_is_never_searched(self):
        result = await self.send("بدي حشوة أسنان اليوم الساعة تسعة صباحا", service_text=FILLING)
        self.assertEqual(self.state()["requested_date"], "2026-11-12")
        self.assertIn("مرقت", result["reply"])
        self.assertEqual(self.reads, [])
        self.assertIsNone(result.get("pending_action"))

    async def test_open_day_offers_other_times_when_none_are_nearby(self):
        self.friday_close = "19:00"
        await self.send(FIRST, service_text="أسنان")
        second = await self.send(FILLING, service_text=FILLING)
        self.assertEqual(second["state"], "slots_proposed")
        self.assertEqual(second["speech_route"], "gemini")
        self.assertIn("مواعيد تانية", second["reply"])
        self.assertEqual([s["start"] for s in self.state()["proposed_slots"]], ["11:00"])
        self.assertIsNone(second.get("pending_action"))

    async def test_spoken_filling_selection(self):
        self.friday_start = "09:00"
        self.friday_extra = ["09:15"]
        welcome = await self.send("بدي أحجز موعد بكرة ع الساعة ثلاثة")
        self.assertEqual(welcome["speech_route"], "gemini")
        offer = await self.send(FILLING, service_text=FILLING)
        self.assertIn("خارج دوام", offer["reply"])
        self.assertEqual(offer["speech_route"], "gemini")
        state = self.state()
        self.assertEqual([slot["start"] for slot in state["proposed_slots"]], ["09:00", "09:15"])
        for phrase in ("الساعة تسعة", "الساعة تسع", "تسعة الصبح", "التاسعة صباحًا", "بدي الساعة تسعة",
                       "موعد التسعة", "تسع مع رانيا", "الساعة تسعة بالضبط", "تسعة", "9:00",
                       "الخيار الأول", "الموعد الأول", "رقم واحد", "الأول"):
            with self.subTest(phrase=phrase):
                reply = await self.send(phrase)
                self.assertEqual(reply["reply"], "شو اسم المريض الكامل؟")
                self.assertEqual(self.state()["selected_slot_id"], state["proposed_slots"][0]["slot_id"])
                session_store.save(state)

        rejected = await self.send("تسع صباحا مع الدكتورة رانيا منصور")
        self.assertEqual(rejected["state"], "awaiting_slot_selection")
        self.assertIsNone(self.state()["selected_slot_id"])
        session_store.save(state)

        transcript = "تسع صباحا مع الدكتورة رانيا خوري"
        with patch("agent.graph.extract_request", return_value=IntakeResult(language="ar", intent="booking")), \
             patch("integrations.voice_adapter.transcribe", return_value=transcript):
            voice = await self.client.post("/api/voice", data={"session_id": self.session, "language_hint": "ar"},
                                           files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})
        self.assertEqual(voice.status_code, 200, voice.text)
        self.assertEqual(voice.json()["reply"], "شو اسم المريض الكامل؟")
        self.assertEqual(voice.json()["speech_route"], "gemini")
        self.assertEqual(self.state()["selected_slot_id"], state["proposed_slots"][0]["slot_id"])

        name = "ليان منصور"
        phone = "0591234567"
        question = await self.send(f"اسمي {name}", patient_name=name)
        self.assertEqual(question["reply"], "شو رقم تلفونك؟")
        self.assertEqual(question["speech_route"], "gemini")
        confirmation = await self.send(f"رقمي {phone}", patient_phone=phone)
        self.assertIn(phone, confirmation["reply"])
        self.assertEqual(confirmation["speech_route"], "gemini")
        self.assertNotIn(phone, tickets[confirmation["speech_token"]][1])
        self.assertIsNone(self.state()["pending_action"])
        review = await self.send("نعم")
        self.assertEqual(review["state"], "awaiting_approval")
        self.assertEqual(review["speech_route"], "gemini")
        self.assertNotIn(name, tickets[review["speech_token"]][1])
        self.assertEqual(self.state()["requested_date"], "2026-11-13")
        self.assertEqual(self.state()["service_id"], "dental_filling")
        self.assertIsNotNone(self.state()["pending_action"])

    async def test_earliest_checkup_to_review_with_typed_and_voice_phone(self):
        self.range_slots = [("2026-11-13", "09:00"), ("2026-11-13", "09:15")]
        self.friday_start = "09:00"
        phone = "0599000111"
        name = "سامر مثال"

        async def speak(transcript, **fields):
            with patch("agent.graph.extract_request", return_value=IntakeResult(language="ar", intent="booking")), \
                 patch("integrations.voice_adapter.transcribe", return_value=transcript) as stt:
                response = await self.client.post("/api/voice", data={"session_id": self.session, "language_hint": "ar", **fields},
                                                  files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})
            stt.assert_called_once()
            return response

        for voice_phone in (False, True):
            with self.subTest(voice_phone=voice_phone):
                session_store.delete(self.session)
                app.state.last_audio.pop(self.session, None)
                await self.send("بدي فحص أسنان دوري", service_text="فحص أسنان دوري")
                slots = await self.send("شو أقرب موعد موجود")
                self.assertEqual(slots["state"], "slots_proposed", slots["reply"])
                self.assertEqual(self.state()["proposed_slots"][0]["start"], "09:00")
                self.assertEqual(self.state()["proposed_slots"][0]["date"], "2026-11-13")
                name_prompt = await self.send("تسعة الصبح")
                self.assertEqual(name_prompt["reply"], "شو اسم المريض الكامل؟")
                self.assertEqual(name_prompt["speech_route"], "gemini")
                phone_prompt = await self.send(f"اسمي {name}", patient_name=name)
                self.assertEqual(phone_prompt["reply"], "شو رقم تلفونك؟")
                self.assertEqual(phone_prompt["speech_route"], "gemini")
                if voice_phone:
                    rejected = await speak("صفر خمسة وبعدها يمكن تسعة")
                    self.assertEqual(rejected.status_code, 422)
                    self.assertEqual(rejected.json()["category"], "phone_transcript_rejected")
                    self.assertEqual(self.state()["patient_name"], name)
                    self.assertTrue(self.state()["selected_slot_id"])
                    response = await speak("صفر خمسة تسعة تسعة صفر صفر صفر واحد واحد واحد")
                    self.assertEqual(response.status_code, 200, response.text)
                    confirmation = response.json()
                else:
                    confirmation = await self.send(phone)
                self.assertIn(phone, confirmation["reply"])
                self.assertEqual(confirmation["state"], "awaiting_phone_confirmation")
                self.assertEqual(confirmation["speech_route"], "gemini")
                self.assertIsNone(self.state()["pending_action"])
                # The fixture WAV is otherwise identical on every mocked voice turn.
                app.state.last_audio.pop(self.session, None)
                review = (await speak("نعم")).json() if voice_phone else await self.send("نعم")
                self.assertEqual(review["state"], "awaiting_approval")
                self.assertEqual(review["appointment_review"]["patient_name"], name)
                self.assertEqual(review["appointment_review"]["masked_phone"], f"•••• {phone[-4:]}")
                self.assertEqual(self.state()["confirmed_phone"], phone)
                self.assertEqual(review["speech_route"], "gemini")
                self.assertIsNotNone(self.state()["pending_action"])
        self.addCleanup(app.state.last_audio.pop, self.session, None)

    async def test_synthetic_phone_probe_is_local_one_shot_and_leaves_intake_unchanged(self):
        state = create_initial_state(self.session)
        state.update(stage="awaiting_patient_details", selected_slot_id="fixture-09:00",
                     patient_name="سامر مثال")
        session_store.save(state)
        transcript = "صفر خمسة وبعدها يمكن تسعة"
        key = "fixture-probe-secret"
        form = {"session_id": self.session, "language_hint": "ar", "synthetic_phone_probe": key}
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as local:
            with patch.dict("os.environ", {"ZAYTOUN_SYNTHETIC_PHONE_PROBE": key}), \
                 patch("integrations.voice_adapter.transcribe", return_value=transcript) as stt:
                async def record(client):
                    return await client.post("/api/voice", data=form,
                                             files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})

                remote = await record(self.client)
                self.assertEqual(remote.status_code, 422)
                self.assertNotIn(transcript, remote.text)
                probe = await record(local)
                self.assertEqual(probe.headers["cache-control"], "no-store")
                self.assertEqual(probe.json(), {"synthetic_phone_transcript": transcript,
                                                "validation": "phone_transcript_rejected", "phone_issue": "format"})
                retry = await record(local)
                self.assertEqual(retry.status_code, 422)
                self.assertNotIn(transcript, retry.text)
                self.assertEqual(stt.call_count, 3)
        self.assertEqual(self.state(), state)
        self.assertNotIn(self.session, app.state.voice_reviews)

    async def test_hours_question_preserves_offer(self):
        self.friday_start = "09:00"
        await self.send(FIRST)
        await self.send(FILLING, service_text=FILLING)
        before = self.state()
        for question in ("شو مواعيد فتح العيادة؟", "شومواعيد فتح العيادة", "ما هي ساعات استقبال العيادة"):
            reply = await self.send(question)
            self.assertIn("الجمعة", reply["reply"])
            self.assertEqual(reply["speech_route"], "gemini")
            self.assertEqual(self.state()["proposed_slots"], before["proposed_slots"])

    async def test_availability_question_is_not_selection(self):
        self.friday_start = "09:00"
        self.friday_extra = ["09:15"]
        await self.send("بدي فحص أسنان بكرة الساعة ثلاثة", service_text="فحص أسنان")
        before = self.state()
        for question in ("الساعة ثلاث العصر في موعد على الساعة ثلاث؟", "في موعد الساعة ثلاث؟"):
            reply = await self.send(question)
            self.assertIn("خارج دوام", reply["reply"])
            self.assertEqual(reply["speech_route"], "gemini")
            self.assertEqual(self.state()["proposed_slots"], before["proposed_slots"])
            self.assertIsNone(self.state()["selected_slot_id"])
        available = await self.send("Is 9 AM available?", language="en")
        self.assertIn("is available", available["reply"])
        unavailable = await self.send("Is 10 AM available?", language="en")
        self.assertIn("is unavailable", unavailable["reply"])
        self.assertEqual(self.state()["proposed_slots"], before["proposed_slots"])
        self.assertIsNone(self.state()["selected_slot_id"])
        reply = await self.send("تسع صباحا مع الدكتورة رانيا خوري")
        self.assertEqual(reply["reply"], "شو اسم المريض الكامل؟")

    async def test_voice_inquiry_keeps_transcript(self):
        self.friday_start = "09:00"
        await self.send(FIRST)
        await self.send(FILLING, service_text=FILLING)
        before = self.state()
        message = "يوم الجمعة لحد أي ساعة فاتحين؟"
        with patch("integrations.voice_adapter.transcribe", return_value=message):
            response = await self.client.post("/api/voice", data={"session_id": self.session, "language_hint": "ar"},
                                              files={"audio": ("fixture.wav", wav_bytes(), "audio/wav")})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["transcript"], message)
        self.assertEqual(self.state()["proposed_slots"], before["proposed_slots"])

    async def test_earliest_after_past_date(self):
        self.friday_start = "09:00"
        past = await self.send("بدي فحص أسنان دوري يوم ١٠ نوفمبر", service_text="فحص أسنان دوري")
        self.assertIn("هالتاريخ مرق", past["reply"])
        for phrase in ("أقرب موعد موجود", "أقرب يوم"):
            result = await self.send(phrase)
            self.assertEqual(result["state"], "slots_proposed", result["reply"])
            self.assertEqual(self.state()["service_id"], "dental_checkup")
            self.assertEqual(self.state()["requested_date"], "2026-11-12")
            self.assertIsNone(self.state()["selected_slot_id"])
            self.assertIsNone(self.state()["pending_action"])


    async def test_earliest_respects_elapsed_closed_and_booked_days(self):
        cases = [
            ("2026-11-12", [("2026-11-12", "09:00"), ("2026-11-12", "10:30")], ["10:30"], "2026-11-12"),
            ("2026-11-12", [("2026-11-11", "14:00"), ("2026-11-12", "09:00"),
                           ("2026-11-14", "11:15"), ("2026-11-14", "11:00")], ["11:00", "11:15"], "2026-11-14"),
            ("2026-11-15", [("2026-11-15", "13:00"), ("2026-11-16", "11:00")], ["11:00"], "2026-11-16"),
            ("2026-11-12", [("2026-12-01", "11:00")], [], None),
        ]
        for today, slots, expected_times, expected_day in cases:
            with self.subTest(today=today, times=expected_times):
                self.today, self.range_slots = today, slots
                state = create_initial_state(self.session)
                state.update(service_id="dental_checkup", service_text="فحص أسنان دوري",
                             stage="awaiting_details", patient_name="Test Patient")
                session_store.save(state)
                data = await self.send("first available slot", language="en")
                updated = self.state()
                self.assertEqual(self.reads[-1]["date_from"], today)
                self.assertEqual(self.reads[-1]["service_id"], "dental_checkup")
                self.assertEqual(updated["requested_date"], expected_day)
                self.assertEqual([slot["start"] for slot in updated["proposed_slots"]], expected_times)
                self.assertEqual(updated["patient_name"], "Test Patient")
                self.assertIsNone(updated["selected_slot_id"])
                self.assertIsNone(updated["pending_action"])
                self.assertEqual(data["speech_route"], "gemini")
                if not expected_times:
                    self.assertIn("No appointments", data["reply"])

    async def test_same_day_offers_follow_configured_clinic_clock(self):
        from services import clinic_api

        async def clinic_read(tool, args):
            if tool == "clinic_info":
                return {"ok": True, **clinic_api.get_clinic()}
            if tool == "list_services":
                return {"ok": True, **clinic_api.list_services()}
            if tool == "find_slots":
                return {"ok": True, **await clinic_api.availability(
                    args["service_id"], args["date_from"], args["date_to"],
                    args.get("after"), args.get("before"), args.get("provider_id"), args["limit"],
                )}
            self.fail(f"Unexpected read {tool}")

        with patch.dict("os.environ", {"CLINIC_TODAY": "2026-11-12", "CLINIC_TIME": "12:00"}), \
             patch.object(clinic_api, "STORE", clinic_api.Store()), \
             patch.object(self, "call_read", side_effect=clinic_read):
            self.assertEqual(clinic_api.get_clinic()["now"], "2026-11-12T12:00:00+02:00")
            available = await clinic_read("find_slots", {
                "service_id": "dental_checkup", "date_from": "2026-11-12",
                "date_to": "2026-11-12", "limit": 50,
            })
            expected = {slot["slot_id"] for slot in available["slots"]}
            self.assertTrue(expected)
            self.assertTrue(all(slot["start"] >= "12:00" for slot in available["slots"]))
            for message, fields in (
                ("بدي فحص أسنان دوري اليوم", {"service_text": "فحص أسنان دوري"}),
                ("شو أقرب موعد موجود", {}),
            ):
                with self.subTest(message=message):
                    result = await self.send(message, **fields)
                    state = self.state()
                    self.assertEqual(result["state"], "slots_proposed")
                    self.assertEqual(state["requested_date"], "2026-11-12")
                    self.assertTrue(state["proposed_slots"])
                    self.assertTrue(all(slot["start"] >= "12:00" and slot["slot_id"] in expected
                                        for slot in state["proposed_slots"]))
                    self.assertIsNone(state["pending_action"])
