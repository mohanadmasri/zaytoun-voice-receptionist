# اختبارات ترقيم المواعيد وربط البحث والاختيار والموافقة عبر HTTP والحالة الحقيقية.
# المدخل رسائل ونتائج أدوات وهمية؛ المخرج assertions للعرض والترتيب والسلامة.
# لا تستخدم LLM أو MCP أو أي API خارجي؛ كل محاولة كتابة تفشل فورًا في البوابة الوهمية.
import re
import unittest
from test_support import confirm_phone_turn
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

import httpx

from app.main import app, get_gateway
from agent.intake import IntakeResult
from agent.slot_selection import select_slot, extract_option_numbers
from agent.state import create_initial_state
from core.session_store import session_store


# تعيد موعدًا وهميًا بمعرف وتاريخ ووقت وطبيب محددين دون توليد موعد من كلام المريض.
def make_slot(slot_id, start, provider="dr_khoury", date="2026-11-03"):
    return {"slot_id": slot_id, "date": date, "start": start,
            "service_id": "dental_checkup", "provider_id": provider,
            "provider_name": "Dr. Rania Khoury" if provider == "dr_khoury" else "Dr. Sami Nassar"}


# قائمة غير مرتبة زمنيًا عمدًا؛ الترقيم يجب أن يحافظ على ترتيب نتيجة التصفية.
SLOTS = [make_slot("SL-A", "16:00"), make_slot("SL-B", "15:45"),
         make_slot("SL-C", "15:45", "dr_nassar")]
COMPACT_TIME_OFFERS = [make_slot("SL-SAMI", "14:15", "dr_nassar"),
                       make_slot("SL-RANIA-LATE", "16:30"),
                       make_slot("SL-RANIA-EARLY", "13:15")]


# تبني حالة صالحة للاختبارات المباشرة دون لمس SessionStore.
def selection_state():
    state = create_initial_state("numbered-unit")
    state.update({"language": "en", "stage": "slots_proposed", "service_id": "dental_checkup",
                  "requested_date": "2026-11-03", "time_kind": "window", "time_after": "15:01",
                  "provider_choice": "any", "proposed_slots": deepcopy(SLOTS)})
    return state


# تفحص تحليل الأرقام وتقاطع الرقم والمعرف والوقت وحدود القائمة دون أي أدوات.
class NumberedSelectionTests(unittest.TestCase):
    def test_compact_clock_matches_only_displayed_offers(self):
        state = selection_state()
        state.update(time_kind="missing", time_after=None, proposed_slots=list(COMPACT_TIME_OFFERS))
        for message, expected in (
            ("115", "SL-RANIA-EARLY"), ("430", "SL-RANIA-LATE"),
            ("1", "SL-SAMI"), ("option 3", "SL-RANIA-EARLY"),
            ("one fifteen", "SL-RANIA-EARLY"), ("four thirty", "SL-RANIA-LATE"),
        ):
            with self.subTest(message=message):
                self.assertEqual(select_slot(state, message)["selected_slot_id"], expected)

        # A real 1:50 offer must not make the previously rejected phrase valid.
        state["proposed_slots"].append(make_slot("SL-FIFTY", "13:50"))
        for message in ("at 150", "345", "option 115", "+115", "0115", "175", "000", "1.15"):
            with self.subTest(message=message):
                result = select_slot(state, message)
                self.assertIsNone(result["selected_slot_id"])
                self.assertEqual(result["stage"], "awaiting_slot_selection")

        state["proposed_slots"].append(make_slot("SL-SAMI-EARLY", "13:15", "dr_nassar"))
        result = select_slot(state, "115")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-RANIA-EARLY", "SL-SAMI-EARLY"])

    def test_bare_hours_resolve_from_actual_date_specific_hours(self):
        from agent.graph import normalize_time_node

        state = {"requested_date": "2026-11-10", "clinic_calendar": {"opening_hours": {
            "tuesday": {"open": "09:00", "close": "19:00"},
        }}}
        for phrase in ("at three", "at 3", "الساعة ثلاثة", "الساعة ٣"):
            with self.subTest(phrase=phrase):
                result = normalize_time_node({**state, "time_text": phrase})
                self.assertEqual((result["time_kind"], result["requested_time"]), ("exact", "15:00"))
        for calendar in ({}, {"opening_hours": {"tuesday": {"open": "00:00", "close": "19:00"}}}):
            with self.subTest(calendar=calendar):
                result = normalize_time_node({**state, "time_text": "at three", "clinic_calendar": calendar})
                self.assertEqual(result["time_kind"], "ambiguous")
                guessed = normalize_time_node({**state, "time_text": "3 PM", "current_message": "at three",
                                               "clinic_calendar": calendar})
                self.assertEqual(guessed["time_kind"], "ambiguous")
        explicit = normalize_time_node({**state, "time_text": "15:00", "current_message": "at 3 AM"})
        self.assertEqual(explicit["requested_time"], "03:00")
        self.assertTrue(explicit["time_outside_hours"])

    def test_arabic_quarter_to_does_not_fall_back_to_the_hour(self):
        state = selection_state()
        state.update(language="ar", time_kind="missing", time_after=None, proposed_slots=[
            {**make_slot("SL-QUARTER-TO", "09:45", "dr_nassar"), "provider_name_ar": "سامي نصار"},
            {**make_slot("SL-HOUR", "10:00", "dr_nassar"), "provider_name_ar": "سامي نصار"},
        ])
        for message, expected in (
            ("عشرة إلى ربع مع الدكتور سامي نصار", "SL-QUARTER-TO"),
            ("بختار الساعة ١٠ إلى ربع", "SL-QUARTER-TO"),
            ("عشرة إلا ربع", "SL-QUARTER-TO"),
            ("العشرة إلا ربع", "SL-QUARTER-TO"),
            ("الساعة عشرة إلا ربع صباحًا", "SL-QUARTER-TO"),
            ("الساعة عشرة إلا ربع مساءً", None),
            ("الساعة عشرة إلا", None),
            ("ثلاثة إلا ربع", None),
        ):
            with self.subTest(message=message):
                result = select_slot(state, message)
                self.assertEqual(result["selected_slot_id"], expected)
                if expected is None:
                    self.assertEqual(result["stage"], "awaiting_slot_selection")

        state["proposed_slots"].append(make_slot("SL-PM", "21:45", "dr_nassar"))
        result = select_slot(state, "عشرة إلا ربع")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-QUARTER-TO", "SL-PM"])

    def test_arabic_relative_times_in_new_searches(self):
        from agent.graph import normalize_time_node
        from agent.times import normalize_time

        for phrase, expected in (("ثلاثة إلا ربع مساءً", "14:45"), ("عشرة ونص صباحًا", "10:30"),
                                 ("عشرة وربع صباحًا", "10:15"), ("واحدة إلا ربع صباحًا", "00:45")):
            with self.subTest(phrase=phrase):
                self.assertEqual(normalize_time(phrase)["requested_time"], expected)
        result = normalize_time_node({"time_text": "10:00 AM", "current_message": "الساعة عشرة إلا"})
        self.assertEqual(result["time_kind"], "unrecognized")
        self.assertIsNone(result["requested_time"])

    def test_english_spoken_minutes_and_ambiguity(self):
        state = selection_state()
        state.update(time_kind="missing", time_after=None, proposed_slots=[
            make_slot("SL-QUARTER", "13:15"), make_slot("SL-FIFTY", "13:50"),
        ])
        self.assertEqual(select_slot(state, "one fifty")["selected_slot_id"], "SL-FIFTY")
        state["proposed_slots"].pop()
        for message in ("at 150 with Dr. Rania Khoury", "one fifteen AM", "one fifty", "two fifteen"):
            with self.subTest(message=message):
                self.assertIsNone(select_slot(state, message)["selected_slot_id"])
        state["proposed_slots"].append(make_slot("SL-AM", "01:15"))
        result = select_slot(state, "one fifteen")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-QUARTER", "SL-AM"])

    def test_english_search_time_uses_known_clinic_hours(self):
        from agent.graph import normalize_time_node

        state = {"time_text": "one fifteen", "requested_date": "2026-11-10",
                 "clinic_calendar": {"opening_hours": {"tuesday": {"open": "09:00", "close": "17:00"}}}}
        self.assertEqual(normalize_time_node(state)["requested_time"], "13:15")
        state["time_text"] = "one fifteen AM"
        self.assertEqual(normalize_time_node(state)["requested_time"], "01:15")
        state.update(time_text="one fifteen", clinic_calendar={})
        self.assertEqual(normalize_time_node(state)["time_kind"], "ambiguous")
        state.update(time_text="1:15 PM", current_message="I want at 150")
        self.assertEqual(normalize_time_node(state)["time_kind"], "unrecognized")

    def test_arabic_spoken_hour_requires_a_unique_current_offer(self):
        state = selection_state()
        state.update(language="ar", time_kind="missing", time_after=None,
                     proposed_slots=[make_slot("SL-AM", "02:00", "dr_nassar"),
                                     make_slot("SL-PM", "14:00", "dr_nassar")])
        result = select_slot(state, "موعد الساعة اثنتين")
        self.assertEqual(result["stage"], "awaiting_slot_selection")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-AM", "SL-PM"])
        state.update(stage=result["stage"], selection_candidates=result["selection_candidates"])
        self.assertEqual(select_slot(state, "مساءً")["selected_slot_id"], "SL-PM")
        self.assertIsNone(select_slot(state, "موعد الساعة ثلاثة")["selected_slot_id"])

    def test_arabic_quarter_hour_requires_a_unique_current_offer(self):
        state = selection_state()
        state.update(language="ar", time_kind="missing", time_after=None)
        morning = {**make_slot("SL-AM", "01:15"), "provider_name_ar": "رانيا خوري"}
        afternoon = {**morning, "slot_id": "SL-PM", "start": "13:15"}
        state["proposed_slots"] = [morning, afternoon]
        result = select_slot(state, "الساعة واحدة وربع مع رانيا خوري")
        self.assertEqual(result["stage"], "awaiting_slot_selection")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-AM", "SL-PM"])
        self.assertEqual(select_slot(state, "واحدة وربع مساءً مع رانيا خوري")["selected_slot_id"], "SL-PM")
        state["proposed_slots"] = [make_slot("SL-OTHER", "16:30")]
        self.assertIsNone(select_slot(state, "واحدة وربع")["selected_slot_id"])

    def test_doctor_and_hour_must_agree_on_one_offered_slot(self):
        state = selection_state()
        self.assertEqual(select_slot(state, "Dr. Rania Khoury at four p m")["selected_slot_id"], "SL-A")
        for message in ("4 PM with Dr. Rania Smith", "4 PM with Dr. Sami Nasser",
                        "4 PM or 3:45 PM with Dr. Rania Khoury"):
            with self.subTest(message=message):
                self.assertIsNone(select_slot(state, message)["selected_slot_id"])
        state["proposed_slots"].append(make_slot("SL-D", "16:00"))
        result = select_slot(state, "4 PM with Dr. Rania Khoury")
        self.assertEqual(result["stage"], "awaiting_slot_selection")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-A", "SL-D"])

    def test_supported_forms(self):
        for message in ("2", "option 2", "الخيار ٢", "I choose option 2", "أختار ٢"):
            with self.subTest(message=message):
                result = select_slot(selection_state(), message)
                self.assertEqual(result["selected_slot_id"], "SL-B")
                self.assertEqual(result["stage"], "slot_selected")

    def test_invalid_conflicting_and_out_of_range(self):
        messages = ("0", "-1", "4", "option 99", "option 2.5", "option banana",
                    "option 1 or 2", "option 1 and option 2", "2 or 3",
                    "option 2 SL-A", "option 2 SL-OLD", "SL-A SL-B",
                    "option 2 at 16:00", "SL-A at 15:45", "15:45 or 16:00",
                    "option 2 from the previous list", "الخيار ٢ من القائمة القديمة")
        for message in messages:
            with self.subTest(message=message):
                result = select_slot(selection_state(), message)
                self.assertEqual(result["stage"], "awaiting_slot_selection")
                self.assertIsNone(result["selected_slot_id"])
                self.assertIsNone(result["pending_action"])

    def test_id_time_agreement_and_ambiguity(self):
        for message in ("SL-B", "option 2 SL-B at 15:45"):
            self.assertEqual(select_slot(selection_state(), message)["selected_slot_id"], "SL-B")
        result = select_slot(selection_state(), "I choose 3:45 PM")
        self.assertEqual(result["stage"], "awaiting_slot_selection")
        self.assertIsNone(result["selected_slot_id"])
        self.assertEqual(result["selection_candidates"], ["SL-B", "SL-C"])
        self.assertIn("Which doctor", result["reply"])
        self.assertNotIn("SL-", result["reply"])

    def test_numbers_use_only_current_list_and_revalidate(self):
        state = selection_state()
        state["proposed_slots"] = [state["proposed_slots"][2], state["proposed_slots"][0]]
        self.assertEqual(select_slot(state, "2")["selected_slot_id"], "SL-A")
        self.assertIsNone(select_slot(state, "SL-B")["selected_slot_id"])
        for field, value in (("date", "2026-11-04"), ("start", "09:00")):
            invalid = selection_state()
            invalid["proposed_slots"][1][field] = value
            self.assertIsNone(select_slot(invalid, "2")["selected_slot_id"])
        self.assertIsNone(extract_option_numbers("My phone is 0599000999"))
        self.assertIsNone(extract_option_numbers("0599000999"))
        self.assertIsNone(extract_option_numbers("15:45"))


# تعيد عمدًا مواعيد خارج الوقت والتاريخ لاختبار دفاع Scheduler، وتسجل أي كتابة ممنوعة.
class FakeGateway:
    def __init__(self):
        self.reads = []
        self.writes = []

    async def call_read(self, name, args):
        self.reads.append((name, args))
        if name == "clinic_info":
            return {"ok": True, "today": "2026-11-02"}
        if name == "list_services":
            return {"ok": True, "services": [{"id": "dental_checkup", "name_en": "Dental check-up"}]}
        if name == "find_slots":
            assert args["after"] == "15:01"
            assert args["date_from"] == args["date_to"]
            if args["date_from"] == "2026-11-04":
                slots = [make_slot("SL-NEW-A", "16:30", date="2026-11-04"),
                         make_slot("SL-NEW-B", "17:00", date="2026-11-04")]
            elif args["date_from"] == "2026-11-05":
                slots = []
            else:
                slots = [make_slot("SL-MORNING", "09:00"), SLOTS[0],
                         make_slot("SL-WRONG-DATE", "16:00", date="2026-11-04"),
                         SLOTS[1], make_slot("SL-BOUNDARY", "15:00"), SLOTS[2]]
            return {"ok": True, "slots": deepcopy(slots)}
        raise AssertionError(f"Unexpected read: {name}")

    async def call_write(self, name, args):
        self.writes.append((name, args))
        raise AssertionError("No booking writes are permitted")


# تختبر API وRunner وGraph وScheduler معًا؛ الاستخراج والعيادة فقط بدائل وهمية.
class NumberedApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.session_id = f"numbered-api-{uuid4().hex}"
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.pop(get_gateway, None)
        session_store.delete(self.session_id)
        app.state.session_locks.pop(self.session_id, None)
        self.assertEqual(self.gateway.writes, [])

    # ترسل رسالة HTTP باستخراج ثابت وتعيد JSON؛ لا تستدعي OpenRouter.
    async def send(self, message, **extracted):
        async def fake_extract(_message):
            return IntakeResult(language="en", intent="booking", **extracted)
        with patch("agent.graph.extract_request", new=fake_extract):
            response = await self.client.post("/api/chat", json={"session_id": self.session_id, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        response = await confirm_phone_turn(self.client, response, self.session_id)
        return response.json()

    # تبدأ المحادثة من حالة فارغة وتثبت انتقال شروط البحث الصحيحة للأداة.
    async def search(self):
        return await self.send("Dental checkup November 3, 2026 after 3 PM, any doctor",
                               service_text="dental checkup", date_text="November 3, 2026",
                               time_text="after 3 PM", provider_any=True)

    def state(self):
        return session_store.get_or_create(self.session_id)

    async def test_search_display_select_and_prepare_approval(self):
        data = await self.search()
        self.assertEqual(data["state"], "slots_proposed")
        self.assertEqual([call["tool"] for call in data["tool_calls"]], ["clinic_info", "list_services", "find_slots"])
        self.assertEqual(self.state()["proposed_slots"], SLOTS)
        lines = [line for line in data["reply"].splitlines() if re.match(r"\d+\.", line)]
        self.assertEqual(lines, [
            "1. 4:00 PM with Dr. Rania Khoury",
            "2. 3:45 PM with Dr. Rania Khoury",
            "3. 3:45 PM with Dr. Sami Nassar"])
        self.assertEqual(data["reply"].count("November 3, 2026"), 1)
        self.assertNotIn("SL-", data["reply"])
        for absent in ("09:00", "15:00", "2026-11-04", "SL-MORNING", "SL-WRONG-DATE"):
            self.assertNotIn(absent, data["reply"])
        self.assertIsNone(data.get("pending_action"))
        reads = len(self.gateway.reads)
        data = await self.send("option 2", time_text="2")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertEqual(self.state()["selected_slot_id"], "SL-B")
        self.assertEqual(self.state()["time_after"], "15:01")
        data = await self.send("My name is Lina Mansour and phone is 0599000999",
                               patient_name="Lina Mansour", patient_phone="0599000999")
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(data["pending_action"]["payload"]["slot_id"], "SL-B")
        self.assertEqual(len(self.gateway.reads), reads)
        self.assertIsNone(data.get("result"))
        old_action = data["pending_action"]["action_id"]
        data = await self.send("الخيار ٣")
        self.assertEqual(data["pending_action"]["payload"]["slot_id"], "SL-C")
        self.assertNotEqual(data["pending_action"]["action_id"], old_action)
        data = await self.send("option 99")
        self.assertEqual(data["state"], "awaiting_slot_selection")
        self.assertIsNone(data.get("pending_action"))
        self.assertEqual(self.state()["selected_slot_id"], "SL-C")

    async def test_all_number_forms_and_conflicts_over_http(self):
        await self.search()
        for message in ("2", "option 2", "الخيار ٢"):
            data = await self.send(message)
            self.assertEqual(data["state"], "awaiting_patient_details")
            self.assertEqual(self.state()["selected_slot_id"], "SL-B")
        for message in ("option 2 SL-A", "option 2 at 16:00", "option 1 or 2", "0", "4", "option 2.5", "SL-OLD"):
            data = await self.send(message)
            self.assertEqual(data["state"], "awaiting_patient_details" if message in ("0", "4") else "awaiting_slot_selection", message)
            self.assertEqual(self.state()["selected_slot_id"], "SL-B", message)
        data = await self.send("I choose 3:45 PM", time_text="3:45 PM")
        self.assertEqual(data["state"], "awaiting_slot_selection")
        self.assertEqual(self.state()["proposed_slots"], SLOTS)
        data = await self.send("SL-C")
        self.assertEqual(self.state()["selected_slot_id"], "SL-C")

    async def test_new_search_replaces_old_number_mapping(self):
        await self.search()
        await self.send("2")
        data = await self.send("Change date to November 4, 2026", date_text="November 4, 2026")
        self.assertEqual(data["state"], "slots_proposed")
        self.assertNotIn("ID: SL-B", data["reply"])
        self.assertIsNone(self.state()["selected_slot_id"])
        for message in ("3", "SL-B", "option 2 SL-B"):
            data = await self.send(message)
            self.assertEqual(data["state"], "awaiting_slot_selection")
            self.assertIsNone(self.state()["selected_slot_id"])
        await self.send("option 2")
        self.assertEqual(self.state()["selected_slot_id"], "SL-NEW-B")
        data = await self.send("Change date to November 5, 2026", date_text="November 5, 2026")
        self.assertEqual(data["state"], "no_slots")
        self.assertEqual(self.state()["proposed_slots"], [])
        self.assertIsNone(self.state()["selected_slot_id"])
        data = await self.send("option 2")
        self.assertIsNone(self.state()["selected_slot_id"])
        self.assertIsNone(data.get("pending_action"))

    async def test_safety_precedes_numbered_selection(self):
        await self.search()
        data = await self.send("I choose option 2, I have severe chest pain")
        self.assertEqual(data["state"], "emergency")
        self.assertEqual(self.state()["proposed_slots"], [])
        self.assertIsNone(self.state()["selected_slot_id"])
        self.assertIsNone(data.get("pending_action"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
