import asyncio
from unittest.mock import patch

from agent.graph import graph
from agent.intake import IntakeResult
from agent.state import create_initial_state

DAY = "2026-11-12"
OFFER = "لقيت مواعيد متاحة ليوم 12 نوفمبر 2026: 3:00 مساءً مع د. رانيا خوري."


def slot(clock, day=DAY, provider="dr_khoury"):
    return {"slot_id": f"{day}-{clock}-{provider}", "date": day, "start": clock,
            "service_id": "dental_checkup", "provider_id": provider,
            "provider_name": "د. رانيا خوري"}


class Gateway:
    def __init__(self, initial, earlier):
        self.initial = initial
        self.earlier = earlier
        self.searches = []

    async def call_read(self, name, args):
        if name == "clinic_info":
            return {"ok": True, "today": DAY, "now": DAY + "T10:00:00",
                    "providers": [{"id": "dr_khoury", "service_ids": ["dental_checkup"]}],
                    "opening_hours": {"thursday": {"open": "11:00", "close": "18:00"}}}
        assert name == "find_slots", name
        self.searches.append(args)
        return {"ok": True, "slots": self.initial if len(self.searches) == 1 else self.earlier}

    async def call_write(self, name, args):
        raise AssertionError("Unexpected write")


async def conversation(message, initial, earlier, *, selected=None, initial_time=None):
    gateway = Gateway(initial, earlier)
    state = create_initial_state("relative-time")
    state.update(service_text="dental checkup", service_id="dental_checkup",
                 provider_id="dr_khoury", provider_choice="specific")

    async def extract(text):
        return IntakeResult(language="en" if text == message and text.isascii() else "ar", intent="booking",
                            date_text="November 12, 2026" if text == "موعد فحص أسنان" else None,
                            time_text=text if text == message else initial_time)

    with patch("agent.graph.extract_request", new=extract):
        first = await graph.ainvoke({**state, "current_message": "موعد فحص أسنان"},
                                    context={"gateway": gateway})
        assert first["stage"] == "slots_proposed", first["reply"]
        assert [item["start"] for item in first["proposed_slots"]] == [item["start"] for item in initial]
        reply = OFFER if len(initial) == 1 and initial[0]["start"] == "15:00" else first["reply"]
        history = [*first["messages"], {"role": "assistant", "content": reply}]
        if selected:
            first.update(selected_slot_id=slot(selected)["slot_id"], stage="slot_selected")
        second = await graph.ainvoke({**first, "messages": history, "current_message": message},
                                     context={"gateway": gateway})
    return second, gateway.searches


async def main():
    earlier = [slot("09:00"), slot("10:30"), slot("14:30"), slot("15:00"), slot("16:00"),
               slot("14:00", "2026-11-13"), slot("14:00", provider="other")]
    for message in ("في مواعيد قبل الساعة ثلاثة؟", "أبكر من الموعد", "قبل هذا الموعد",
                    "earlier than 3", "anything before that time", "before 3 PM"):
        state, searches = await conversation(message, [slot("15:00")], earlier)
        assert state["stage"] == "slots_proposed", (message, state["reply"])
        assert len(searches) == 2 and searches[-1]["before"] == "15:00", searches
        assert searches[-1]["date_from"] == searches[-1]["date_to"] == DAY
        assert searches[-1]["provider_id"] == "dr_khoury"
        assert searches[-1]["service_id"] == "dental_checkup"
        assert [item["start"] for item in state["proposed_slots"]] == ["14:30"]
        assert state["requested_date"] == DAY and state["service_id"] == "dental_checkup"
        assert state["provider_id"] == "dr_khoury"
    state, searches = await conversation("قبل هذا الموعد", [slot("15:00")], [])
    assert state["stage"] == "no_slots" and not state["proposed_slots"]
    assert len(searches) == 2
    state, searches = await conversation("anything before that time", [slot("14:00"), slot("15:00")], earlier)
    assert len(searches) == 1 and state["stage"] == "awaiting_slot_selection"
    state, searches = await conversation("earlier than 3", [slot("14:00")], earlier)
    assert len(searches) == 1 and state["stage"] == "awaiting_slot_selection"
    state, searches = await conversation("anything before that time", [slot("14:00"), slot("15:00")],
                                         earlier, selected="15:00")
    assert searches[-1]["before"] == "15:00" and state["stage"] == "slots_proposed"
    assert state["selected_slot_id"] is None and state["pending_action"] is None
    state, searches = await conversation("before 3 PM", [slot("15:00")],
                                         earlier + [slot("13:30")], initial_time="after 2 PM")
    assert searches[-1]["after"] == "14:01" and searches[-1]["before"] == "15:00"
    assert [item["start"] for item in state["proposed_slots"]] == ["14:30"]
    state, searches = await conversation("في مواعيد قبل الساعة ثلاثة؟", [slot("15:00")],
                                         earlier, initial_time="3 PM")
    assert searches[0]["after"] == "15:00" and searches[0]["before"] == "15:01"
    assert "after" not in searches[-1] and searches[-1]["before"] == "15:00"
    assert [item["start"] for item in state["proposed_slots"]] == ["14:30"]
    print("Relative-time graph regressions passed")


if __name__ == "__main__":
    asyncio.run(main())


