# يختبر متابعة اختيار موعد داخل Graph باستخدام بيانات وهمية واستخراج بديل.
# المدخل رسائل وحالات ثابتة؛ المخرج assertions ونتائج الاختبارات.
# لا يستخدم LLM أو MCP أو API؛ يمنع القراءات والكتابات الخارجية.
"""Test the graph's slot-selection follow-up without OpenRouter or real clinic writes.
Run inside kit: python test_slot_graph_followup.py
"""
import asyncio
from copy import deepcopy
from unittest.mock import patch

from agent.graph import graph, route_after_intake
from agent.intake import IntakeResult, update_state_from_intake
from agent.state import create_initial_state


class NoWritesGateway:
    def __init__(self):
        self.reads = []
        self.writes = []

    async def call_read(self, name, args):
        self.reads.append(name)
        raise AssertionError(f"Unexpected read: {name}")

    async def call_write(self, name, args):
        self.writes.append(name)
        raise AssertionError("Booking must not be written before approval")


# تستقبل الحالة والرسالة ونتيجة استخراج ثابتة وتعيد الحالة بعد Graph.
# تفحص سجل Gateway لأن بعض العقد تلتقط الاستثناءات وتحوّلها لحالة خطأ.
async def send(state, message, extracted):
    state["current_message"] = message

    async def fake_extract(_message):
        return extracted

    gateway = NoWritesGateway()
    with patch("agent.graph.extract_request", new=fake_extract):
        updated = await graph.ainvoke(state, context={"gateway": gateway})
    assert gateway.reads == [], gateway.reads
    assert gateway.writes == [], gateway.writes
    return updated


# تشغّل حالات الالتباس والاختيار والبيانات والموافقة والسلامة وتغيير البحث.
async def main():
    state = create_initial_state("slot_graph_followup_test")
    state.update({
        "language": "en", "stage": "slots_proposed", "intent": "booking",
        "service_text": "dental checkup", "service_id": "dental_checkup",
        "date_text": "November 3, 2026", "requested_date": "2026-11-03",
        "time_text": "after 3 PM", "time_kind": "window", "time_after": "15:01",
        "time_before": None, "provider_choice": "any", "provider_id": None,
        "proposed_slots": [
            {"slot_id": "SL-TEST-A", "date": "2026-11-03", "start": "15:30",
             "provider_id": "dr_khoury", "provider_name": "Dr. Rania Khoury",
             "service_id": "dental_checkup"},
            {"slot_id": "SL-TEST-B", "date": "2026-11-03", "start": "15:45",
             "provider_id": "dr_khoury", "provider_name": "Dr. Rania Khoury",
             "service_id": "dental_checkup"},
            {"slot_id": "SL-TEST-C", "date": "2026-11-03", "start": "15:45",
             "provider_id": "dr_nassar", "provider_name": "Dr. Sami Nassar",
             "service_id": "dental_checkup"},
        ],
    })

    initial = deepcopy(state)
    state = await send(state, "I choose 3:45 PM", IntakeResult(
        language="en", intent="booking", time_text="3:45 PM"))
    assert state["stage"] == "awaiting_slot_selection", state
    assert state.get("selected_slot_id") is None
    assert len(state["proposed_slots"]) == 3
    print("PASSED: ambiguous time preserves proposed slots")

    state = await send(state, "I choose SL-TEST-A", IntakeResult(
        language="en", intent="booking", time_text="15:30"))
    assert state["stage"] == "awaiting_patient_details", state
    assert state["selected_slot_id"] == "SL-TEST-A", state
    assert len(state["proposed_slots"]) == 3
    print("PASSED: selected slot survives LLM time extraction")

    state = await send(state, "My name is Test Patient and phone is 0599000999", IntakeResult(
        language="en", intent="booking", patient_name="Test Patient",
        patient_phone="0599000999"))
    assert state["stage"] == "awaiting_approval", state
    assert state["selected_slot_id"] == "SL-TEST-A"
    assert state["pending_action"]["type"] == "create_booking"
    assert state["pending_action"]["payload"]["slot_id"] == "SL-TEST-A"
    print("PASSED: patient details create pending approval, no booking write")

    action_id = state["pending_action"]["action_id"]
    state = await send(state, "Please show the approval again", IntakeResult(
        language="en", intent="booking"))
    assert state["pending_action"]["action_id"] == action_id
    print("PASSED: unchanged pending approval keeps its action ID")

    corrected = await send(deepcopy(state), "My name is Corrected Patient", IntakeResult(
        language="en", intent="booking", patient_name="Corrected Patient"))
    assert corrected["selected_slot_id"] == "SL-TEST-A"
    assert corrected["pending_action"]["action_id"] != action_id
    assert corrected["pending_action"]["payload"]["patient_name"] == "Corrected Patient"
    print("PASSED: corrected patient details replace old pending approval")

    state = await send(state, "I have severe chest pain", IntakeResult(
        language="en", intent="booking", safety="none"))
    assert state["stage"] == "emergency", state
    assert state.get("pending_action") is None
    print("PASSED: emergency overrides pending approval")

    missing = await send(deepcopy(initial), "I choose 9:00 PM", IntakeResult(
        language="en", intent="booking", time_text="9:00 PM"))
    assert missing["stage"] == "awaiting_slot_selection"
    assert missing["selected_slot_id"] is None
    assert len(missing["proposed_slots"]) == 3
    print("PASSED: unlisted selection cannot create a booking approval")

    mismatched = deepcopy(initial)
    mismatched["proposed_slots"][0]["date"] = "2026-11-04"
    rejected = await send(mismatched, "I choose SL-TEST-A", IntakeResult(
        language="en", intent="booking"))
    assert rejected["stage"] == "awaiting_slot_selection"
    assert rejected["selected_slot_id"] is None
    assert rejected["pending_action"] is None
    print("PASSED: a slot on the wrong date is rejected")

    children = deepcopy(initial)
    children["service_id"] = "pediatrics_visit"
    children["proposed_slots"][0]["service_id"] = "pediatrics_visit"
    children["patient_name"] = "Test Child"
    children["patient_phone"] = "0599000999"
    blocked = await send(children, "I choose SL-TEST-A", IntakeResult(
        language="en", intent="booking"))
    assert blocked["stage"] == "awaiting_patient_details"
    assert blocked["pending_action"] is None
    print("PASSED: guardian verification cannot be bypassed")

    for changes in (
        {"date_text": "November 4, 2026"},
        {"service_text": "dental cleaning"},
        {"provider_text": "Dr. Sami Nassar"},
        {"time_text": "after 5 PM"},
    ):
        previous = deepcopy(initial)
        previous["current_message"] = "Change my appointment search"
        updated = update_state_from_intake(previous, IntakeResult(
            language="en", intent="booking", **changes))
        assert updated["proposed_slots"] == [], changes
        assert updated["selected_slot_id"] is None, changes
        assert updated["pending_action"] is None, changes
        assert route_after_intake(updated) == "normal", changes
        assert len(previous["proposed_slots"]) == 3
    print("PASSED: changed search conditions invalidate old slots")

    for extracted, expected in (
        (IntakeResult(language="en", intent="booking", injection_detected=True), "blocked_injection"),
        (IntakeResult(language="unsupported"), "unsupported_language"),
    ):
        updated = await send(deepcopy(initial), "I choose 3:45 PM", extracted)
        assert updated["stage"] == expected
        assert updated.get("pending_action") is None
    print("PASSED: injection and unsupported language override slot routing")
    print("ALL GRAPH FOLLOW-UP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
