import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from actions.approval import handle_approval, prepare_booking_action
from agent.graph import appointment_followup_node, slot_followup_node
from agent.intake import IntakeResult
from agent.runner import handle_message
from agent.scheduler import schedule
from agent.slot_handoff import slot_matches_offer
from agent.slot_selection import select_slot
from agent.state import create_initial_state
from core.session_store import SessionStore


def booking_state(**updates):
    return {
        "stage": "slots_proposed",
        "language": "ar",
        "service_id": "consultation",
        "requested_date": "2026-11-11",
        "provider_choice": "any",
        "time_kind": "missing",
        "proposed_slots": [
            {"slot_id": "SL-1", "date": "2026-11-11", "start": "15:20", "provider_id": "doctor"},
        ],
        **updates,
    }


class StabilizationBookingTests(unittest.IsolatedAsyncioTestCase):
    async def test_past_day_and_same_day_elapsed_time_do_not_search(self):
        for day, clock in (("2026-11-10", "15:20"), ("2026-11-11", "15:20")):
            with self.subTest(day=day):
                gateway = SimpleNamespace(call_read=AsyncMock(return_value={"ok": True, "slots": []}))
                state = booking_state(
                    stage="needs_slot_lookup", requested_date=day, time_kind="exact",
                    requested_time=clock, clinic_today="2026-11-11",
                    clinic_calendar={"now": "2026-11-11T15:21:00+02:00"},
                )
                result = await schedule(state, gateway)
                self.assertFalse(result["proposed_slots"])
                gateway.call_read.assert_not_awaited()

    async def test_elapsed_slot_is_invalid_for_selection_or_approval(self):
        state = booking_state(clinic_calendar={"now": "2026-11-11T15:21:00+02:00"})
        self.assertFalse(slot_matches_offer(state, state["proposed_slots"][0]))

    async def test_offer_expires_after_the_clock_advances(self):
        gateway = SimpleNamespace(call_read=AsyncMock(return_value={
            "ok": True, "today": "2026-11-11", "now": "2026-11-11T15:20:10+02:00",
        }))
        state = booking_state(current_message="تمام", clinic_calendar={"now": "2026-11-11T15:19:00+02:00"})
        result = await appointment_followup_node(state, SimpleNamespace(context={"gateway": gateway}))
        self.assertEqual(result["stage"], "slot_unavailable")
        self.assertIsNone(result.get("pending_action"))
        self.assertFalse(result["proposed_slots"])

    async def test_explicit_approval_refreshes_the_clock_before_writing(self):
        state = booking_state(
            session_id="expired-approval", stage="awaiting_approval", selected_slot_id="SL-1",
            patient_name="Lina Haddad", patient_phone="0599000111", confirmed_phone="0599000111",
            clinic_calendar={"now": "2026-11-11T15:19:00+02:00"},
        )
        state["pending_action"] = prepare_booking_action(state)
        store = SessionStore()
        store.save(state)
        gateway = SimpleNamespace(
            call_read=AsyncMock(return_value={
                "ok": True, "today": "2026-11-11", "now": "2026-11-11T15:20:10+02:00",
            }),
            call_write=AsyncMock(side_effect=AssertionError("Booking write blocked")),
        )
        with patch("actions.approval.session_store", store):
            result = await handle_approval(state["session_id"], state["pending_action"]["action_id"], True, gateway)
        self.assertEqual(result["state"], "slot_unavailable")
        self.assertIsNone(store.get_or_create(state["session_id"]).get("pending_action"))
        gateway.call_write.assert_not_awaited()

    async def test_service_rejects_the_elapsed_part_of_the_current_minute(self):
        from services import clinic_api as clinic

        day = date(2026, 11, 3)
        now = datetime(2026, 11, 3, 16, 30, 10, tzinfo=clinic.STORE.tz)
        booking = clinic.BookingIn(
            slot_id="SL-20261103-1630-dr_khoury", service_id="dental_checkup",
            patient={"name": "Lina Haddad", "phone": "0599000111"},
        )
        with patch.object(clinic.STORE, "now", return_value=now), \
             patch.object(clinic.STORE, "apply_fault", new=AsyncMock(return_value=None)), \
             patch.object(clinic.STORE, "_free", return_value=True), \
             patch.object(clinic.STORE, "_add", side_effect=AssertionError("Booking write blocked")):
            slots = clinic.STORE.free_slots(clinic.STORE.services[booking.service_id], day,
                                            ["dr_khoury"], 16 * 60 + 30, 16 * 60 + 31)
            self.assertEqual(slots, [])
            with self.assertRaises(clinic.ApiError) as error:
                await clinic.create_booking(booking, SimpleNamespace(headers={}))
            self.assertIn("past", str(error.exception))

    async def test_natural_spoken_arabic_clock_with_minutes(self):
        state = booking_state()
        selected = select_slot(state, "بدي السّاعة تلاتة وعشرين دقيقة المسا")
        self.assertEqual(selected["selected_slot_id"], "SL-1")

    async def test_spoken_one_forty_five_matches_only_a_unique_clock(self):
        state = booking_state()
        state["proposed_slots"][0]["start"] = "13:45"
        message = "بدي الموعد على الساعة واحدة وخمس وأربعين دقيقة"
        self.assertEqual(select_slot(state, message)["selected_slot_id"], "SL-1")
        state["proposed_slots"].append({**state["proposed_slots"][0], "slot_id": "SL-2", "start": "01:45"})
        self.assertIsNone(select_slot(state, message)["selected_slot_id"])

    async def test_voice_transcripts_route_through_the_graph_to_patient_details(self):
        clock = "بدي الموعد على الساعة واحدة وخمس وأربعين دقيقة"
        store = SessionStore()
        gateway = SimpleNamespace(
            call_read=AsyncMock(side_effect=AssertionError("Selection should use the offered slot")),
            call_write=AsyncMock(side_effect=AssertionError("Booking write blocked")),
        )
        intake = AsyncMock(return_value=IntakeResult(language="ar", intent="booking", time_text="واحدة وخمس وأربعين دقيقة"))
        with patch("agent.runner.session_store", store), patch("agent.graph.extract_request", intake):
            for index, (message, language) in enumerate(((clock, "ar"), ("آه مناسب", "ar"), ("تمام", "ar"),
                                                         ("بدي هذا الموعد", "ar"), ("yes", "en"))):
                with self.subTest(message=message):
                    session = f"stabilization-transcript-{index}"
                    state = {**create_initial_state(session), **booking_state()}
                    state["proposed_slots"][0]["start"] = "13:45"
                    store.save(state)
                    response = await handle_message(session, message, gateway, input_language=language)
                    self.assertEqual(response["state"], "awaiting_patient_details")
                    self.assertEqual(store.get_or_create(session)["selected_slot_id"], "SL-1")
                    self.assertIsNone(response.get("pending_action"))
        gateway.call_read.assert_not_awaited()
        gateway.call_write.assert_not_awaited()

    async def test_natural_agreement_advances_to_details_only_for_one_offer(self):
        for message in ("آه مناسب", "تمام", "بدي هذا الموعد", "yes", "تمام، هاد الموعد بناسبني"):
            with self.subTest(message=message):
                state = booking_state(current_message=message)
                result = slot_followup_node(state)
                self.assertEqual(result["stage"], "awaiting_patient_details")
                self.assertEqual(result["selected_slot_id"], "SL-1")
                self.assertIsNone(result["pending_action"])
                state["proposed_slots"].append({**state["proposed_slots"][0], "slot_id": "SL-2", "start": "16:00"})
                self.assertIsNone(slot_followup_node(state).get("selected_slot_id"))
        state = booking_state()
        state["current_message"] = "تمام بس مش هاد الموعد"
        self.assertIsNone(slot_followup_node(state).get("selected_slot_id"))
