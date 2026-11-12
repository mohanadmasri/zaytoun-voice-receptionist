"""Replay reported conversations through the receptionist API without external calls."""

import json
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from agent.intake import IntakeResult
from app.main import app, get_gateway
from core.session_store import session_store
from services import clinic_api, clinic_mcp
from test_numbered_slots import make_slot
from test_voice_api import wav_bytes


WEDNESDAY = "I want a dental checkup on Wednesday."
TUESDAY = "بدي فحص أسنان دوري يوم الثلاثاء الساعة 3"
FIRST = "I want option number one."
CHANGES = (
    "I want to change to option at 11 o'clock.",
    "I want the option at 11 o'clock.",
    "I want the 11 o'clock slot with Rania.",
    "I want the 11 o'clock slot with Dr. Rania Khoury.",
)
ARABIC_CHOICE = "موعد الساعة اثنتين"
NAME = "My name is Maya Haddad."
PHONE = "My phone number is 0591234567."
ARABIC_BOOKING = ("بدي أحجز موعد فحص أسنان دوري يوم الأربعاء", "بدي الموعد الأول",
                  "بدي أغير الموعد للساعة 12 وربع", "ليان ربا", "0581234567", "نعم")
ENGLISH_BOOKING = (WEDNESDAY, FIRST, "I want to change the option at 11 o'clock.",
                   "Layan Rabba", "0581234567", "yes")


class ConversationClinic:
    """Use real clinic reads, with optional availability for the reported offer lists."""

    def __init__(self, client):
        self.client = client
        self.offers = None
        self.writes = []

    async def call_read(self, tool, args):
        if tool == "find_slots" and self.offers is not None:
            slots = [slot for slot in self.offers
                     if args["date_from"] <= slot["date"] <= args["date_to"]
                     and args["service_id"] == slot["service_id"]
                     and args.get("provider_id") in (None, slot["provider_id"])
                     and (args.get("after") or "") <= slot["start"] < (args.get("before") or "24:00")]
            return {"ok": True, "slots": slots[:args["limit"]]}
        path = {"clinic_info": "/api/clinic", "list_services": "/api/services",
                "find_slots": "/api/availability", "get_patient": "/api/patients/{phone}"}[tool]
        params = {key: value for key, value in args.items() if value is not None}
        response = await self.client.get(path.format(**args), params=params if tool == "find_slots" else None)
        return {"ok": response.is_success, "status": response.status_code, **response.json()}

    async def call_write(self, tool, args):
        self.writes.append(tool)
        raise AssertionError(f"Unexpected write: {tool}")


class RealisticConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = []
        clinic_client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=clinic_api.app), base_url="http://clinic.test",
            headers={"X-API-Key": clinic_api.API_KEY},
        ))
        self.gateway = ConversationClinic(clinic_client)
        self.clinic_client = clinic_client
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://receptionist.test",
        ))
        self.enterContext(patch.dict(app.dependency_overrides, {get_gateway: lambda: self.gateway}))
        self.enterContext(patch.object(clinic_api.Store, "now", return_value=datetime(
            2026, 11, 5, 8, tzinfo=ZoneInfo("Asia/Jerusalem"),
        )))
        self.enterContext(patch.object(clinic_api, "STORE", clinic_api.Store()))
        self.enterContext(patch("agent.graph.extract_request", new=self.extract))
        self.enterContext(patch("integrations.voice_adapter.VoiceAdapter.synthesize", return_value={}))

    async def asyncTearDown(self):
        for session in self.sessions:
            session_store.delete(session)
            for store in (app.state.session_locks, app.state.voice_reviews,
                          app.state.voice_generations, app.state.last_audio):
                store.pop(session, None)
        self.assertEqual(self.gateway.writes, [])

    async def extract(self, message):
        fields = {
            WEDNESDAY: dict(service_text="dental checkup", date_text="Wednesday"),
            TUESDAY: dict(service_text="فحص أسنان دوري", date_text="يوم الثلاثاء", time_text="الساعة 3"),
            ARABIC_CHOICE: dict(time_text="الساعة اثنتين"),
            NAME: dict(patient_name="Maya Haddad"),
            PHONE: dict(patient_phone="0591234567"),
            ARABIC_BOOKING[0]: dict(service_text="فحص أسنان دوري", date_text="يوم الأربعاء"),
            ARABIC_BOOKING[2]: dict(time_text="12 وربع"),
            ARABIC_BOOKING[3]: dict(patient_name="ليان ربا"),
            ENGLISH_BOOKING[2]: dict(time_text="11 o'clock"),
            ENGLISH_BOOKING[3]: dict(patient_name="Layan Rabba"),
        }.get(message, {})
        if message in CHANGES:
            fields = {"time_text": "11 o'clock"}
            if "Rania" in message:
                fields["provider_text"] = "Dr. Rania Khoury" if "Dr." in message else "Rania"
        language = "ar" if message in (TUESDAY, ARABIC_CHOICE, *ARABIC_BOOKING) else "en"
        if message == ARABIC_BOOKING[4]:
            language = "unknown"
        return IntakeResult(language=language, intent="booking", **fields)

    def new_session(self):
        session = f"realistic-{uuid4().hex}"
        self.sessions.append(session)
        return session

    async def turn(self, session, message, mode="text", language="en"):
        if mode == "voice":
            with patch("integrations.voice_adapter.transcribe", return_value=message):
                preview = await self.client.post("/api/voice", data={
                    "session_id": session, "language_hint": language, "preview_only": "true",
                }, files={"audio": ("sample.wav", wav_bytes() + uuid4().bytes, "audio/wav")})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["transcript"], message)
            response = await self.client.post("/api/voice/confirm", json={
                "session_id": session, "transcript_token": preview.json()["transcript_token"],
            })
        else:
            response = await self.client.post("/api/chat", json={"session_id": session, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        state = session_store.get_or_create(session)
        pending = state.get("pending_action")
        print(json.dumps({
            "input": message, "mode": mode, "reply": data["reply"], "stage": state["stage"],
            "service": state.get("service_id"), "date": state.get("requested_date"),
            "time": state.get("requested_time"), "selected": state.get("selected_slot_id"),
            "offers": [(s["slot_id"], s["start"], s["provider_id"]) for s in state.get("proposed_slots", [])],
            "name": state.get("patient_name"), "phone": state.get("patient_phone"),
            "confirmed_phone": state.get("confirmed_phone"), "candidates": state.get("selection_candidates"),
            "time_change_pending": state.get("time_change_pending"), "pending": pending and pending["payload"]["slot_id"],
        }), flush=True)
        return state

    def set_offers(self, day, times):
        self.gateway.offers = [make_slot(f"SL-{index}", start, provider, day)
                               for index, (start, provider) in enumerate(times, 1)]

    def assert_search(self, state, day, time=None, order=None):
        self.assertEqual(state["stage"], "slots_proposed")
        self.assertEqual((state["service_id"], state["requested_date"], state.get("requested_time")),
                         ("dental_checkup", day, time))
        self.assertIsNone(state.get("selected_slot_id"))
        self.assertIsNone(state.get("pending_action"))
        if order:
            self.assertEqual([slot["slot_id"] for slot in state["proposed_slots"]], order)

    def assert_slot(self, state, expected):
        selected = next((slot for slot in state["proposed_slots"]
                         if slot["slot_id"] == state.get("selected_slot_id")), None)
        self.assertEqual(selected, expected)
        if selected:
            self.assertEqual(selected["date"], state["requested_date"])
            if "service_id" in selected:
                self.assertEqual(selected["service_id"], state["service_id"])

    async def test_a_english_change(self):
        day = "2026-11-11"
        starts = ("10:30", "10:45", "11:00", "12:00", "12:15")
        offer_ids = [f"SL-20261111-{start.replace(':', '')}-dr_khoury" for start in starts]
        for mode in ("text", "voice"):
            for phrase in (*CHANGES, "11 o'clock"):
                with self.subTest(mode=mode, phrase=phrase):
                    session = self.new_session()
                    search = await self.turn(session, WEDNESDAY, mode)
                    self.assert_search(search, day, order=offer_ids)
                    self.assertEqual([(slot["start"], slot["provider_id"]) for slot in search["proposed_slots"]],
                                     [(start, "dr_khoury") for start in starts])
                    offers = search["proposed_slots"]
                    self.assert_slot(await self.turn(session, FIRST, mode), offers[0])
                    state = await self.turn(session, phrase, mode)
                    self.assertIsNone(state.get("pending_action"))
                    self.assert_slot(state, offers[2])

    async def test_complete_bilingual_bookings(self):
        # MCP tool functions use their real HTTP mappings against a private ASGI clinic.
        async def clinic_request(method, path, *, params=None, body=None, headers=None):
            response = await self.clinic_client.request(
                method, path, params={key: value for key, value in (params or {}).items() if value is not None},
                json=body, headers=headers,
            )
            return {"ok": response.is_success, "status": response.status_code, **response.json()}

        async def clinic_read(tool, args):
            return await getattr(clinic_mcp, tool)(**args)

        async def clinic_write(tool, args):
            self.assertIn(tool, ("create_booking", "save_patient"))
            return await getattr(clinic_mcp, tool)(**args)

        for language, phrases, target in (("ar", ARABIC_BOOKING, "12:15"), ("en", ENGLISH_BOOKING, "11:00")):
            for mode in ("text", "voice"):
                with (
                    self.subTest(language=language, mode=mode),
                    patch.object(clinic_api, "STORE", clinic_api.Store()),
                    patch.object(clinic_mcp, "_call", new=clinic_request),
                    patch.object(self.gateway, "call_read", new=clinic_read),
                ):
                    await self.complete_booking(phrases, target, mode, language, clinic_write)

    async def complete_booking(self, phrases, target, mode, language, clinic_write):
        session = self.new_session()
        request, choice, change, name, phone, confirmation = phrases
        search = await self.turn(session, request, mode, language)
        self.assert_search(search, "2026-11-11")
        self.assertEqual(search["language"], language)
        offers = search["proposed_slots"]
        matching = [slot for slot in offers if slot["start"] == target]
        self.assertEqual(len(matching), 1, offers)
        selected, = matching
        self.assertEqual(selected["provider_id"], "dr_khoury")

        for phrase, expected in ((choice, offers[0]), (change, selected), (name, selected), (phone, selected)):
            state = await self.turn(session, phrase, mode, language)
            self.assert_slot(state, expected)
            self.assertEqual(state["service_id"], "dental_checkup")
            self.assertEqual(state["language"], language)
            self.assertEqual(state["proposed_slots"], offers)
            self.assertIsNone(state.get("pending_action"))
            self.assertFalse(clinic_api.STORE.created)
            if phrase in (name, phone):
                self.assertEqual(state["patient_name"], name)
            if phrase == phone:
                self.assertEqual(state["patient_phone"], phone)
                self.assertIsNone(state.get("confirmed_phone"))
                self.assertEqual(state["stage"], "awaiting_phone_confirmation")

        state = await self.turn(session, confirmation, mode, language)
        self.assert_slot(state, selected)
        self.assertEqual(state["stage"], "awaiting_approval")
        self.assertEqual(state["language"], language)
        self.assertEqual(state["confirmed_phone"], phone)
        action = state["pending_action"]
        self.assertEqual(action["type"], "create_booking")
        payload = action["payload"]
        self.assertEqual(
            {key: payload[key] for key in ("slot_id", "service_id", "patient_name", "patient_phone")},
            {"slot_id": selected["slot_id"], "service_id": "dental_checkup", "patient_name": name, "patient_phone": phone},
        )
        self.assertFalse(clinic_api.STORE.created)
        approval = {"session_id": session, "action_id": action["action_id"], "approved": True}
        with patch.object(self.gateway, "call_write", new=clinic_write):
            response = await self.client.post("/api/approve", json=approval)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["state"], "booking_confirmed", data)
        booking_id, = clinic_api.STORE.created
        booking = (await self.clinic_client.get(f"/api/bookings/{booking_id}")).json()
        self.assertEqual(
            {key: booking[key] for key in ("date", "start", "provider_id", "service_id", "patient")},
            {"date": "2026-11-11", "start": target, "provider_id": "dr_khoury",
             "service_id": "dental_checkup", "patient": {"name": name, "phone": phone}},
        )
        self.assertIn(booking_id, data["reply"])
        self.assertIsNone(session_store.get_or_create(session).get("pending_action"))
        print(json.dumps({"mode": mode, "language": language, "approval": data["reply"],
                          "slot_id": selected["slot_id"], "booking_id": booking_id, "booking_count": 1}), flush=True)

    async def test_oclock_clarification_preserves_selection_and_identity(self):
        day = "2026-11-11"
        self.set_offers(day, [("10:30", "dr_khoury"), ("11:00", "dr_khoury"), ("11:00", "dr_nassar")])
        for mode in ("text", "voice"):
            with self.subTest(mode=mode):
                session = self.new_session()
                for phrase in (WEDNESDAY, FIRST, NAME, PHONE):
                    await self.turn(session, phrase, mode)
                before = await self.turn(session, "yes", mode)
                old_action = before["pending_action"]
                self.assertEqual(old_action["payload"]["slot_id"], "SL-1")

                for phrase, candidates in (
                    ("11 o'clock", ["SL-2", "SL-3"]),
                    ("I want the 9 o'clock slot with Rania", []),
                    ("option number banana at 11 o'clock", []),
                    ("option 99 at 11 o'clock", []),
                ):
                    with self.subTest(phrase=phrase):
                        after = await self.turn(session, phrase, mode)
                        self.assertEqual(after["stage"], "awaiting_slot_selection")
                        self.assertEqual(after["selection_candidates"], candidates)
                        self.assert_slot(after, self.gateway.offers[0])
                        self.assertIsNone(after["pending_action"])
                        for field in ("service_id", "requested_date", "patient_name", "patient_phone",
                                      "confirmed_phone", "proposed_slots"):
                            self.assertEqual(after[field], before[field], field)

                changed = await self.turn(session, "I want the 11 o'clock slot with Rania", mode)
                self.assert_slot(changed, self.gateway.offers[1])
                new_action = changed["pending_action"]
                self.assertEqual(new_action["payload"]["slot_id"], "SL-2")
                self.assertNotEqual(new_action["action_id"], old_action["action_id"])

    async def test_b_arabic_selection(self):
        day = "2026-11-10"
        self.set_offers(day, [("14:15", "dr_nassar"), ("14:00", "dr_nassar"), ("13:45", "dr_nassar"),
                              ("16:30", "dr_khoury"), ("13:15", "dr_khoury")])
        for mode in ("text", "voice"):
            with self.subTest(mode=mode):
                session = self.new_session()
                self.assert_search(await self.turn(session, TUESDAY, mode, "ar"), day, "15:00",
                                   ["SL-1", "SL-2", "SL-3", "SL-4", "SL-5"])
                state = await self.turn(session, ARABIC_CHOICE, mode, "ar")
                self.assert_slot(state, self.gateway.offers[1])
                self.assertIsNone(state.get("pending_action"))

    async def test_c_compact_and_spoken_selection(self):
        day = "2026-11-11"
        self.set_offers(day, [("14:15", "dr_nassar"), ("16:30", "dr_khoury"), ("13:15", "dr_khoury")])
        for mode in ("text", "voice"):
            for phrase, option in (("115", 3), ("430", 2), ("one fifteen", 3), ("four thirty", 2),
                                   ("option 1", 1), ("option 3", 3), ("at 150", None)):
                with self.subTest(mode=mode, phrase=phrase):
                    session = self.new_session()
                    self.assert_search(await self.turn(session, WEDNESDAY, mode), day,
                                       order=["SL-1", "SL-2", "SL-3"])
                    state = await self.turn(session, phrase, mode)
                    self.assert_slot(state, self.gateway.offers[option - 1] if option else None)
                    self.assertEqual(state["stage"], "awaiting_patient_details" if option else "awaiting_slot_selection")
                    self.assertIsNone(state.get("pending_action"))

    async def test_d_identity_and_stale_approval_with_real_clinic(self):
        # No availability fixture here. The real clinic determines the displayed slots.
        for mode in ("text", "voice"):
            with self.subTest(mode=mode):
                session = self.new_session()
                search = await self.turn(session, WEDNESDAY, mode)
                self.assert_search(search, "2026-11-11")
                first, second, *_ = search["proposed_slots"]
                self.assert_slot(await self.turn(session, FIRST, mode), first)
                named = await self.turn(session, NAME, mode)
                self.assertEqual(named["patient_name"], "Maya Haddad")
                self.assertIsNone(named.get("pending_action"))
                phone = await self.turn(session, PHONE, mode)
                self.assertEqual(phone["stage"], "awaiting_phone_confirmation")
                self.assertEqual(phone["patient_phone"], "0591234567")
                self.assertIsNone(phone.get("pending_action"))
                before = await self.turn(session, "yes", mode)
                old_action = before["pending_action"]
                self.assertEqual(old_action["payload"]["slot_id"], first["slot_id"])
                self.assertEqual(before["confirmed_phone"], "0591234567")
                after = await self.turn(session, "option 2", mode)
                self.assert_slot(after, second)
                new_action = after["pending_action"]
                self.assertNotEqual(new_action["action_id"], old_action["action_id"])
                self.assertEqual(new_action["payload"]["slot_id"], second["slot_id"])
                for field in ("service_id", "requested_date", "patient_name", "patient_phone", "confirmed_phone"):
                    self.assertEqual(after[field], before[field], field)
                stale = await self.client.post("/api/approve", json={
                    "session_id": session, "action_id": old_action["action_id"], "approved": True,
                })
                self.assertEqual(stale.status_code, 200, stale.text)
                self.assertIn("No matching pending action", stale.json()["reply"])
                self.assertEqual(session_store.get_or_create(session)["pending_action"], new_action)
                unresolved = await self.turn(session, "option 99", mode)
                self.assert_slot(unresolved, second)
                self.assertIsNone(unresolved.get("pending_action"))
                for field in ("service_id", "requested_date", "patient_name", "patient_phone", "confirmed_phone"):
                    self.assertEqual(unresolved[field], after[field], field)
