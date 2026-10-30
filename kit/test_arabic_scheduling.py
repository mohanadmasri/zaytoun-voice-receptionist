# اختبارات انحدار للنص العربي الفعلي من الميكروفون حتى البحث واختيار الموعد والموافقة.
# المدخل transcript ونتائج Intake مماثلة للاستخراج الفعلي؛ المخرج تحقق من الحالة ومعاملات MCP.
# نستبدل LLM وSTT والعيادة؛ لا نستخدم خدمة خارجية أو ننشئ حجزًا حقيقيًا.
import io
import json
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.main import app, get_gateway
from agent.dates import normalize_date
from agent.intake import IntakeResult
from agent.service_mapping import match_service, normalize_service_text
from agent.times import normalize_time
from core.session_store import session_store


ARABIC_TRANSCRIPT = "بدي اعمل فحص اسنان يوم الاثنين 2 نوفمبر 2026 بعد الساعة 3 العصر عند اي دكتور متاح"
ENGLISH_TRANSCRIPT = "I want a dental checkup on November 2nd 2026 after 3 PM any doctor is fine."
SERVICES = json.loads((Path(__file__).parent / "services/data/services.json").read_text(encoding="utf-8"))


# تختبر التاريخ والخدمة والوقت مباشرة لتحديد موضع الخطأ دون الاعتماد على LLM أو STT.
class ArabicNormalizationTests(unittest.TestCase):
    def test_arabic_calendar_variants(self):
        for text in ("2 نوفمبر 2026", "يوم الاثنين 2 نوفمبر", "٢ نوفمبر ٢٠٢٦",
                     "الاثنين 2 نوفمبر 2026", "يوم الإثنين ٢ نوفمبر ٢٠٢٦",
                     "2 تشرين الثاني 2026", "يوم الاثنين، 2 نوفمبر 2026"):
            with self.subTest(text=text):
                self.assertEqual(normalize_date(text, "2026-11-02"), "2026-11-02")

    def test_invalid_past_and_conflicting_dates(self):
        for text in ("31 نوفمبر 2026", "2 نوفمبر 2025", "الثلاثاء 2 نوفمبر 2026",
                     "32 نوفمبر", "2 شهر مجهول 2026"):
            with self.subTest(text=text):
                self.assertIsNone(normalize_date(text, "2026-11-02"))
        self.assertIsNone(normalize_date("2 نوفمبر", "2026-11-03"))
        self.assertEqual(normalize_date("2 نوفمبر", "2025-11-01"), "2025-11-02")

    def test_existing_english_iso_and_weekdays(self):
        for text in ("November 2nd 2026", "November 2, 2026", "2 November 2026", "2026-11-02"):
            self.assertEqual(normalize_date(text, "2026-11-02"), "2026-11-02")
        self.assertEqual(normalize_date("يوم الاثنين", "2026-11-02"), "2026-11-09")
        self.assertEqual(normalize_date("next Tuesday", "2026-11-02"), "2026-11-03")

    def test_time_and_service_variants(self):
        for text in ("بعد الساعة 3 العصر", "بعد الساعة ٣ العصر", "after 3 PM"):
            self.assertEqual(normalize_time(text)["after"], "15:01")
        for text in ("فحص اسنان", "فحص أسنان", "فحص الاسنان", "فحص الأسنان", "dental checkup"):
            self.assertEqual(match_service(text, SERVICES), "dental_checkup", text)
        for text in ("تنظيف الأسنان", "تنظيف أسنان"):
            self.assertEqual(match_service(text, SERVICES), "dental_cleaning")
        self.assertEqual(normalize_service_text("الأسنان"), "الاسنان")
        self.assertIsNone(match_service("خدمة غير موجودة", SERVICES))
        ambiguous = [{"id": "one", "name_ar": "فحص أسنان"}, {"id": "two", "name_ar": "فحص الأسنان"}]
        self.assertIsNone(match_service("فحص الاسنان", ambiguous))


# تعيد مواعيد بعضها صباحي أو بتاريخ خاطئ كي نختبر التصفية بعد التطبيع أيضًا.
class FakeGateway:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.available = True

    async def call_read(self, name, args):
        self.reads.append((name, args))
        if name == "clinic_info":
            return {"ok": self.available, "today": "2026-11-02"}
        if name == "list_services":
            return {"ok": True, "services": SERVICES}
        if name == "find_slots":
            assert args["date_from"] == args["date_to"] == "2026-11-02"
            assert args["after"] == "15:01"
            assert "provider_id" not in args
            rows = [("09:00", "dr_khoury", "2026-11-02"), ("15:30", "dr_khoury", "2026-11-02"),
                    ("15:45", "dr_khoury", "2026-11-02"), ("15:45", "dr_nassar", "2026-11-02"),
                    ("16:00", "dr_khoury", "2026-11-02"), ("16:00", "dr_nassar", "2026-11-02"),
                    ("16:30", "dr_khoury", "2026-11-03")]
            return {"ok": True, "slots": [
                {"slot_id": f"SL-AR-{index}", "date": day, "start": start, "provider_id": provider,
                 "provider_name": "Dr. Rania Khoury" if provider == "dr_khoury" else "Dr. Sami Nassar",
                 "service_id": "dental_checkup"}
                for index, (start, provider, day) in enumerate(rows)
            ]}
        raise AssertionError(f"Unexpected tool: {name}")

    async def call_write(self, name, args):
        self.writes.append((name, args))
        raise AssertionError("Booking writes are prohibited")


# تختبر HTTP وRunner وGraph الحقيقيين مع استخراج ثابت مماثل للنتيجة المرصودة فعليًا.
class ArabicGraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.sessions = []
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.pop(get_gateway, None)
        for session_id in self.sessions:
            session_store.delete(session_id)
            app.state.session_locks.pop(session_id, None)
        self.assertEqual(self.gateway.writes, [])

    # تنشئ جلسة مستقلة لكل بحث؛ الرسائل اللاحقة تُمرر نفس المعرف صراحةً.
    def new_session(self):
        session_id = f"arabic-regression-{uuid4().hex}"
        self.sessions.append(session_id)
        return session_id

    # تقدم نتيجة Intake مفحوصة بـPydantic دون اتصال OpenRouter وتعيد رد HTTP.
    async def send(self, session_id, message, result):
        async def fake_extract(_message):
            self.assertEqual(_message, message)
            return result
        with patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/chat", json={"session_id": session_id, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_exact_arabic_transcript_and_variants_reach_scheduler(self):
        variants = [("الاثنين 2 نوفمبر 2026", "فحص اسنان"), ("2 نوفمبر 2026", "فحص أسنان"),
                    ("يوم الاثنين 2 نوفمبر", "فحص الاسنان"), ("٢ نوفمبر ٢٠٢٦", "فحص الأسنان")]
        for date_text, service_text in variants:
            with self.subTest(date_text=date_text, service_text=service_text):
                session_id = self.new_session()
                result = IntakeResult(language="ar", intent="booking", service_text=service_text,
                                      date_text=date_text, time_text="بعد الساعة 3 العصر", provider_any=True)
                data = await self.send(session_id, ARABIC_TRANSCRIPT, result)
                self.assertEqual(data["state"], "slots_proposed", data)
                state = session_store.get_or_create(session_id)
                self.assertEqual(state["requested_date"], "2026-11-02")
                self.assertEqual(state["service_id"], "dental_checkup")
                self.assertEqual(state["time_after"], "15:01")
                self.assertEqual(state["provider_choice"], "any")
                self.assertIsNone(state["provider_id"])
                self.assertEqual(len(state["proposed_slots"]), 5)
                self.assertTrue(all(slot["date"] == "2026-11-02" and slot["start"] > "15:00"
                                    for slot in state["proposed_slots"]))
                self.assertNotIn("09:00", data["reply"])
                self.assertNotIn("2026-11-03", data["reply"])
                self.assertIsNone(data.get("pending_action"))
                self.assertIsNone(data.get("result"))

    async def test_arabic_session_selection_and_approval_are_preserved(self):
        session_id = self.new_session()
        await self.send(session_id, ARABIC_TRANSCRIPT, IntakeResult(language="ar", intent="booking",
                        service_text="فحص الاسنان", date_text="الاثنين 2 نوفمبر 2026",
                        time_text="بعد الساعة 3 العصر", provider_any=True))
        data = await self.send(session_id, "الخيار الثاني", IntakeResult(language="ar", intent="booking"))
        self.assertEqual(data["state"], "awaiting_patient_details")
        selected_id = session_store.get_or_create(session_id)["selected_slot_id"]
        self.assertEqual(selected_id, "SL-AR-2")
        data = await self.send(session_id, "اسمي مريضة اختبار ورقمي 0599000999",
                               IntakeResult(language="ar", intent="booking", patient_name="مريضة اختبار",
                                            patient_phone="0599000999"))
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(data["pending_action"]["payload"]["slot_id"], selected_id)
        self.assertEqual(session_store.get_or_create(session_id)["requested_date"], "2026-11-02")
        self.assertIsNone(data.get("result"))

    async def test_english_transcript_is_unchanged(self):
        data = await self.send(self.new_session(), ENGLISH_TRANSCRIPT, IntakeResult(language="en", intent="booking",
                               service_text="dental checkup", date_text="November 2nd 2026",
                               time_text="after 3 PM", provider_any=True))
        self.assertEqual(data["state"], "slots_proposed")

    async def test_voice_transcript_reaches_scheduler_without_stt_changes(self):
        recording = io.BytesIO()
        with wave.open(recording, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)

        async def fake_extract(message):
            self.assertEqual(message, ARABIC_TRANSCRIPT)
            return IntakeResult(language="ar", intent="booking", service_text="فحص اسنان",
                                date_text="الاثنين 2 نوفمبر 2026", time_text="بعد الساعة 3 العصر", provider_any=True)

        with patch("integrations.voice_adapter.transcribe", return_value=ARABIC_TRANSCRIPT), \
             patch("integrations.voice_adapter.speak", return_value=None), \
             patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/voice", data={"session_id": self.new_session()},
                                              files={"audio": ("arabic.wav", recording.getvalue(), "audio/wav")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transcript"], ARABIC_TRANSCRIPT)
        self.assertEqual(response.json()["state"], "slots_proposed")
        self.assertIsNone(response.json().get("pending_action"))

    async def test_unsafe_conflicting_and_unavailable_requests_do_not_search(self):
        for changes, stage in (({"safety": "emergency"}, "emergency"),
                               ({"injection_detected": True}, "blocked_injection"),
                               ({"date_text": "الثلاثاء 2 نوفمبر 2026"}, "awaiting_details"),
                               ({"date_text": "2 نوفمبر 2025"}, "awaiting_details"),
                               ({"language": "unsupported"}, "unsupported_language")):
            self.gateway.reads.clear()
            fields = dict(language="ar", intent="booking", service_text="فحص أسنان",
                          date_text="2 نوفمبر 2026", time_text="بعد الساعة 3 العصر", provider_any=True)
            fields.update(changes)
            data = await self.send(self.new_session(), ARABIC_TRANSCRIPT, IntakeResult(**fields))
            self.assertEqual(data["state"], stage, changes)
            self.assertFalse(any(name == "find_slots" for name, _ in self.gateway.reads))
            self.assertIsNone(data.get("pending_action"))
        self.gateway.available = False
        data = await self.send(self.new_session(), ARABIC_TRANSCRIPT, IntakeResult(language="ar", intent="booking",
                               service_text="فحص أسنان", date_text="2 نوفمبر 2026", time_text="بعد الساعة 3 العصر",
                               provider_any=True))
        self.assertEqual(data["state"], "clinic_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
