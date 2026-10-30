"""Final Developer A handoff/approval smoke test using a fake clinic gateway.

Place in kit/test_final_handoff.py and run from kit: python test_final_handoff.py
No OpenRouter calls or real bookings are made.
"""

import asyncio

from agent.state import create_initial_state
from agent.slot_handoff import build_find_slots_args, slot_matches_request
from actions.approval import prepare_booking_action, handle_approval
from core.session_store import session_store


class FakeGateway:
    def __init__(self):
        self.writes = []

    async def call_read(self, tool_name, args):
        assert tool_name == "find_slots"
        slots = [
            {"slot_id": "SL-TEST-1630", "date": "2026-11-03", "start": "16:30",
             "provider_id": "dr_nassar", "provider_name": "Dr. Sami Nassar"},
            {"slot_id": "SL-TEST-1700", "date": "2026-11-03", "start": "17:00",
             "provider_id": "dr_nassar", "provider_name": "Dr. Sami Nassar"},
        ]
        found = [
            slot for slot in slots
            if args["date_from"] <= slot["date"] <= args["date_to"]
            and (args["provider_id"] is None or slot["provider_id"] == args["provider_id"])
            and (args["after"] is None or slot["start"] >= args["after"])
            and (args["before"] is None or slot["start"] < args["before"])
        ]
        return {"ok": True, "service_id": args["service_id"], "slots": found[:args["limit"]]}

    async def call_write(self, tool_name, args):
        assert tool_name == "create_booking"
        self.writes.append(args)
        return {"ok": True, "booking_id": "BK-TEST-100"}


async def main():
    gateway = FakeGateway()
    state = create_initial_state("final_handoff_test")
    state.update({
        "stage": "needs_slot_lookup",
        "service_id": "dental_checkup",
        "requested_date": "2026-11-03",
        "time_kind": "exact",
        "requested_time": "16:30",
        "provider_choice": "specific",
        "provider_id": "dr_nassar",
        "patient_name": "Test Patient",
        "patient_phone": "0599000999",
    })

    # 1. Correct time search parameters.
    args = build_find_slots_args(state)
    result = await gateway.call_read("find_slots", args)
    assert result["ok"] is True and len(result["slots"]) == 1
    assert result["slots"][0]["start"] == "16:30"
    print("PASSED: Exact requested time found")

    # 2. Prepare an approval only for a matching slot.
    selected = result["slots"][0]
    assert slot_matches_request(state, selected)
    state["proposed_slots"] = result["slots"]
    state["selected_slot_id"] = selected["slot_id"]
    state["stage"] = "awaiting_approval"
    pending = prepare_booking_action(state)
    state["pending_action"] = pending
    session_store.save(state)
    print("PASSED: Matching slot prepared for approval")

    # 3. Approval creates exactly one simulated booking.
    response = await handle_approval(state["session_id"], pending["action_id"], True, gateway)
    assert response["state"] == "booking_confirmed" and len(gateway.writes) == 1
    print("PASSED: Approved booking executed once")

    await handle_approval(state["session_id"], pending["action_id"], True, gateway)
    assert len(gateway.writes) == 1
    print("PASSED: Duplicate booking prevented")

    # 4. If exact time unavailable, alternative slots are NOT automatically accepted.
    state["stage"] = "needs_slot_lookup"
    state["requested_time"] = "16:45"
    exact_args = build_find_slots_args(state)
    exact_result = await gateway.call_read("find_slots", exact_args)
    assert exact_result["slots"] == []
    alternative_args = {**exact_args, "after": "16:00", "before": "18:00"}
    alternatives = await gateway.call_read("find_slots", alternative_args)
    assert alternatives["slots"]
    assert all(not slot_matches_request(state, slot) for slot in alternatives["slots"])
    print("PASSED: Alternatives require patient selection")

    print("ALL FINAL HANDOFF TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
