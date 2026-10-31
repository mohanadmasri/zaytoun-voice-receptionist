# انحدار المحادثة العربية والاختيار الطبيعي والعرض دون معرفات داخلية.
# يستخدم HTTP وGraph الحقيقيين مع بدائل LLM وSTT وMCP، وكل كتابة حجز ممنوعة.
# فحص JavaScript يحاكي إعدادات speechSynthesis فقط؛ لا يثبت سماع صوت في متصفح حقيقي.
import json
import re
import shutil
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.main import app, get_gateway
from agent.intake import IntakeResult
from agent.dates import normalize_date
from agent.service_mapping import match_service
from agent.times import normalize_time
from agent.state import create_initial_state
from agent.slot_selection import select_slot, extract_option_numbers
from core.session_store import session_store
from test_voice_api import wav_bytes
from test_voice_ui import app_ui

DATA = Path(__file__).parent / "services/data"
SERVICES = json.loads((DATA / "services.json").read_text(encoding="utf-8"))
PROVIDERS = json.loads((DATA / "providers.json").read_text(encoding="utf-8"))
TRANSCRIPT = "بدي موعد دكتور اسنان يوم اثنين 2 نوفمبر 2026 على الساعة 3 بعد العصر عند اي دكتور متاح"


# بوابة قراءة وهمية تعرض كتالوج العيادة الحقيقي ومواعيد اختبار؛ لا تنفذ أي كتابة.
class ReadOnlyGateway:
    def __init__(self):
        self.reads = []
        self.writes = []

    async def call_read(self, name, args):
        self.reads.append((name, args))
        if name == "clinic_info":
            return {"ok": True, "today": "2026-11-02", "providers": PROVIDERS}
        if name == "list_services":
            return {"ok": True, "services": SERVICES}
        if name == "get_patient":
            return {"ok": True, "name": "مريضة اختبار", "preferred_provider_id": None}
        if name == "find_slots":
            rows = [("09:00", "dr_khoury"), ("15:00", "dr_khoury"), ("15:30", "dr_khoury"),
                    ("15:45", "dr_khoury"), ("15:45", "dr_nassar"), ("16:00", "dr_khoury"), ("16:00", "dr_nassar")]
            return {"ok": True, "slots": [
                {"slot_id": f"SL-CONV-{i}", "date": args["date_from"], "start": time,
                 "provider_id": provider, "service_id": args["service_id"],
                 "provider_name": next(p["name"] for p in PROVIDERS if p["id"] == provider)}
                for i, (time, provider) in enumerate(rows)
            ]}
        raise AssertionError(name)

    async def call_write(self, name, args):
        self.writes.append(name)
        raise AssertionError("No bookings may be written")


# تتحقق من المعنى قبل Graph: إجراءات مختلفة تبقى مختلفة والاختصاص العام لا يصبح فحصًا.
class ConversationNormalizationTests(unittest.TestCase):
    def test_colloquial_services_and_ambiguity(self):
        for text in ("بدي أعمل فحص أسنان", "بدي أكشف على أسناني", "بدي اكشف على سناني"):
            self.assertEqual(match_service(text, SERVICES), "dental_checkup", text)
        self.assertEqual(match_service("بدي موعد تنظيف أسنان", SERVICES), "dental_cleaning")
        self.assertEqual(match_service("بدي حشوة اسنان", SERVICES), "dental_filling")
        for text in ("بدي موعد عند دكتور أسنان", "بدي أحجز عند طبيب الأسنان", "فحص وتنظيف أسنان", "بدي تبييض أسنان"):
            self.assertIsNone(match_service(text, SERVICES), text)
        self.assertIsNone(match_service("بدي أكشف على أسناني", [s for s in SERVICES if s["id"] != "dental_checkup"]))
        self.assertIsNone(match_service("فحص أسنان", [*SERVICES, {"id": "another_exam", "name_ar": "فحص أسنان"}]))

    def test_dates_and_times(self):
        self.assertEqual(normalize_date("يوم اثنين ٢ نوفمبر ٢٠٢٦", "2026-11-02"), "2026-11-02")
        for value in ("3 بعد العصر", "على الساعة 3 بعد العصر", "٣ بعد الظهر"):
            self.assertEqual(normalize_time(value)["requested_time"], "15:00", value)
        self.assertEqual(normalize_time("بعد الساعة 3 العصر")["after"], "15:01")
        self.assertEqual(normalize_time("تلاتة وربع العصر")["requested_time"], "15:15")
        self.assertEqual(normalize_time("3:45 PM")["requested_time"], "15:45")
        self.assertEqual(normalize_time("3:45")["kind"], "ambiguous")

    def test_ordinal_forms(self):
        for value in ("The second one", "option two", "الخيار الثاني", "بدي أختار الموعد الثاني"):
            self.assertEqual(extract_option_numbers(value), [2], value)

    def test_unavailable_doctor_time_is_not_silently_substituted(self):
        state = create_initial_state("no-substitution")
        state.update({"language": "ar", "provider_catalog": PROVIDERS,
                      "service_id": "dental_checkup", "provider_choice": "any",
                      "requested_date": "2026-11-02", "time_kind": "window", "time_after": "15:01",
                      "proposed_slots": [
                          {"slot_id": "SL-SAMI", "date": "2026-11-02", "start": "15:45", "provider_id": "dr_nassar", "provider_name": "Dr. Sami Nassar"},
                          {"slot_id": "SL-RANIA", "date": "2026-11-02", "start": "17:00", "provider_id": "dr_khoury", "provider_name": "Dr. Rania Khoury"}]})
        result = select_slot(state, "بدي الساعة 3:45 مع الدكتورة رانيا")
        self.assertEqual(result["stage"], "awaiting_slot_selection")
        self.assertIsNone(result["selected_slot_id"])
        self.assertNotIn("SL-", result["reply"])
        state["proposed_slots"] = [state["proposed_slots"][0]]
        result = select_slot(state, "اسمي سامي")
        self.assertIsNone(result["selected_slot_id"])


# تختبر استمرار الجلسة والعرض والاختيار عبر API مع حفظ المعرف داخليًا فقط.
class ArabicConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = ReadOnlyGateway()
        self.session_id = "conversation-" + uuid4().hex
        self.extracted = IntakeResult(language="ar", intent="booking")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        app.dependency_overrides[get_gateway] = lambda: self.gateway

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.pop(get_gateway, None)
        session_store.delete(self.session_id)
        app.state.session_locks.pop(self.session_id, None)
        self.assertEqual(self.gateway.writes, [])

    # ترسل رسالة بنفس الجلسة وتستبدل استخراج LLM فقط، فتختبر باقي المسار الحقيقي.
    async def send(self, message, language="ar", **fields):
        result = IntakeResult(language=language, intent="booking", **fields)
        async def fake_extract(_message):
            return result
        with patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/chat", json={"session_id": self.session_id, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertNotIn("SL-", data["reply"])
        self.assertNotIn("dr_khoury", data["reply"])
        self.assertNotIn("dental_checkup", data["reply"])
        return data

    def state(self):
        return session_store.get_or_create(self.session_id)

    # تبحث عن فحص محدد بعد الثالثة، مع خمس خيارات ثابتة للاختبار دون إنشاء مواعيد في العيادة.
    async def search(self, language="ar"):
        return await self.send("بدي أعمل فحص أسنان يوم اثنين 2 نوفمبر 2026 بعد الساعة 3 العصر",
                               language=language, service_text="فحص أسنان", date_text="يوم اثنين 2 نوفمبر 2026",
                               time_text="بعد الساعة 3 العصر", provider_any=True)

    async def test_exact_failure_is_short_catalog_clarification_not_guessed_service(self):
        state = create_initial_state(self.session_id)
        state.update({"patient_name": "مريضة اختبار", "patient_phone": "0599000999"})
        session_store.save(state)
        data = await self.send(TRANSCRIPT, service_text="دكتور اسنان", date_text="2 نوفمبر 2026",
                               time_text="3 بعد العصر", provider_any=True)
        self.assertEqual(data["state"], "awaiting_details")
        self.assertEqual(data["reply"].count("؟"), 1)
        for label in ("فحص", "تنظيف", "حشوة"):
            self.assertIn(label, data["reply"])
        self.assertNotIn("لم أتمكن", data["reply"])
        self.assertIsNone(self.state()["service_id"])
        self.assertEqual(self.state()["requested_date"], "2026-11-02")
        self.assertEqual(self.state()["requested_time"], "15:00")
        self.assertEqual(self.state()["patient_phone"], "0599000999")
        self.assertFalse(any(name == "find_slots" for name, _ in self.gateway.reads))
        data = await self.send("بدي أكشف على أسناني", service_text="أكشف على أسناني")
        self.assertEqual(data["state"], "slots_proposed")
        self.assertEqual(self.state()["service_id"], "dental_checkup")
        self.assertEqual(self.state()["requested_date"], "2026-11-02")
        self.assertEqual(len(self.state()["proposed_slots"]), 1)

    async def test_generic_dentist_without_date_asks_service_first(self):
        for phrase in ("بدي موعد عند دكتور أسنان", "بدي أحجز عند طبيب الأسنان"):
            data = await self.send(phrase, service_text=phrase)
            self.assertEqual(data["state"], "awaiting_details")
            self.assertIn("تنظيف", data["reply"])
            self.assertIsNone(self.state()["service_id"])

    async def test_arabic_and_english_lists_are_natural_and_keep_ids_only_in_state(self):
        data = await self.search()
        self.assertEqual(data["language"], "ar")
        self.assertEqual(data["reply"].count("2 نوفمبر 2026"), 1)
        self.assertEqual(len(re.findall(r"^\d+\.", data["reply"], re.MULTILINE)), 5)
        self.assertIn("3:45 مساءً مع د. رانيا خوري", data["reply"])
        self.assertNotRegex(data["reply"], r"[A-Za-z]")
        ids = [slot["slot_id"] for slot in self.state()["proposed_slots"]]
        data = await self.send("The second one", language="en")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertIn("full name", data["reply"])
        self.assertEqual(self.state()["selected_slot_id"], ids[1])
        self.assertEqual([slot["slot_id"] for slot in self.state()["proposed_slots"]], ids)
        data = await self.send("اسمي مريضة اختبار ورقمي 0599000999", patient_name="مريضة اختبار", patient_phone="0599000999")
        self.assertEqual(data["state"], "awaiting_approval")
        action = data["pending_action"]
        self.assertEqual(action["payload"]["slot_id"], ids[1])
        self.assertNotIn("dental_checkup", action["summary"])
        self.assertNotIn("SL-", action["summary"])
        self.assertIn("فحص", action["summary"])
        self.assertIn("رانيا", action["summary"])
        data = await self.send("Please show the approval again", language="en")
        self.assertEqual(data["pending_action"]["action_id"], action["action_id"])
        self.assertIn("Dental check-up", data["pending_action"]["summary"])
        self.assertIn("Please review", data["reply"])
        self.assertEqual(self.state()["patient_phone"], "0599000999")

    async def test_partial_doctor_names_select_exact_offered_slot(self):
        await self.search()
        original = [slot["slot_id"] for slot in self.state()["proposed_slots"]]
        for message, language, provider_text in (
            ("بدي الساعة 3:45 مع الدكتورة رانيا", "ar", "الدكتورة رانيا"),
            ("الساعة ٣:٤٥ عند الدكتورة رانيا", "ar", "رانيا"),
            ("3:45 with Dr. Rania", "en", "Dr. Rania"),
        ):
            data = await self.send(message, language=language, time_text="3:45", provider_text=provider_text)
            self.assertEqual(data["state"], "awaiting_patient_details", message)
            selected = next(slot for slot in self.state()["proposed_slots"] if slot["slot_id"] == self.state()["selected_slot_id"])
            self.assertEqual((selected["start"], selected["provider_id"]), ("15:45", "dr_khoury"))
            self.assertEqual([slot["slot_id"] for slot in self.state()["proposed_slots"]], original)
            self.assertEqual(self.state()["provider_choice"], "any")

    async def test_only_missing_patient_detail_is_requested(self):
        await self.search()
        data = await self.send("الخيار الثاني")
        self.assertEqual(data["reply"], "شو اسم المريض الكامل؟")
        data = await self.send("اسمي مريضة اختبار", patient_name="مريضة اختبار")
        self.assertEqual(data["reply"], "شو رقم تلفونك؟")
        selected_id = self.state()["selected_slot_id"]
        data = await self.send("رقمي 0599000999", patient_phone="0599000999")
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(data["pending_action"]["payload"]["slot_id"], selected_id)
        data = await self.send("غير رقم التلفون إلى 0599000888", patient_phone="0599000888")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertIsNone(self.state()["patient_name"])
        self.assertIsNone(data.get("pending_action"))

    async def test_ambiguous_time_then_doctor_reply_preserves_candidate_context(self):
        await self.search()
        data = await self.send("بدي الساعة 3:45", time_text="3:45")
        self.assertEqual(data["state"], "awaiting_slot_selection")
        self.assertIn("رانيا", data["reply"])
        self.assertIn("سامي", data["reply"])
        self.assertEqual(len(self.state()["selection_candidates"]), 2)
        data = await self.send("الدكتورة رانيا", provider_text="الدكتورة رانيا")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertEqual(self.state()["selected_slot_id"], "SL-CONV-3")
        self.assertEqual(self.state()["selection_candidates"], [])

    async def test_conflicts_safety_and_explicit_approval(self):
        await self.search()
        for message in ("option 2 with Dr. Sami", "3:45 with Dr. Nobody", "option two or three", "0"):
            data = await self.send(message)
            self.assertEqual(data["state"], "awaiting_slot_selection", message)
            self.assertIsNone(self.state()["selected_slot_id"])
        await self.send("بدي أختار الموعد الثاني")
        data = await self.send("اسمي مريضة اختبار ورقمي 0599000999", patient_name="مريضة اختبار", patient_phone="0599000999")
        action_id = data["pending_action"]["action_id"]
        data = await self.send("نعم")
        self.assertEqual(data["state"], "awaiting_approval")
        response = await self.client.post("/api/approve", json={"session_id": self.session_id, "action_id": action_id, "approved": False})
        self.assertIn("ما تم أي حجز", response.json()["reply"])
        data = await self.send("عندي ألم شديد بالصدر", safety="emergency")
        self.assertEqual(data["state"], "emergency")
        self.assertIsNone(data.get("pending_action"))

    async def test_voice_and_language_hint(self):
        async def fake_extract(message):
            return IntakeResult(language="ar", intent="booking", service_text="فحص اسنان", date_text="2 نوفمبر 2026",
                                time_text="بعد الساعة 3 العصر", provider_any=True)
        with patch("integrations.voice_adapter.transcribe", return_value=TRANSCRIPT), \
             patch("integrations.voice_adapter.speak", return_value=None), patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/voice", data={"session_id": self.session_id},
                                              files={"audio": ("ar.wav", wav_bytes(), "audio/wav")})
            response = await self.client.post("/api/voice/confirm", json={"session_id": self.session_id, "transcript_token": response.json()["transcript_token"]})
        data = response.json()
        self.assertEqual(data["state"], "slots_proposed")
        self.assertEqual(data["transcript"], TRANSCRIPT)
        self.assertEqual(data["language"], "ar")
        self.assertNotIn("SL-", data["reply"])
        speech = json.loads(app_ui.show(data, [])[8])
        self.assertEqual(speech["language"], "ar")


# ينفذ JavaScript الخاص بالواجهة داخل Node مع speechSynthesis وهمي، وليس سماعات حقيقية.
class BrowserSpeechConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_language_voice_selection_and_no_voice_fallback(self):
        script = """
        const spoken=[];
        global.SpeechSynthesisUtterance=class {constructor(text){this.text=text;}};
        global.speechSynthesis={cancel(){},getVoices(){return [{lang:'ar-JO'},{lang:'en-GB'}]},speak(u){spoken.push(u.lang)},
          addEventListener(){},removeEventListener(){}};
        global.window={speechSynthesis};
        const speak=REPLACE_FUNCTION;
        (async()=>{
          await speak(JSON.stringify({text:'موعدك متاح',language:'ar'}));
          await speak(JSON.stringify({text:'Your appointment with رانيا',language:'en'}));
          speechSynthesis.getVoices=()=>[];
          global.setTimeout=(fn)=>{fn()};
          await speak(JSON.stringify({text:'مرحبا',language:'ar'}));
          console.log(JSON.stringify(spoken));
        })();
        """.replace("REPLACE_FUNCTION", app_ui.SPEAK_JS)
        result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["ar-JO", "en-GB", "ar-PS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
