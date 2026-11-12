"""Resolve an earlier-appointment request against verified conversation offers."""

import re

from agent.presentation import say
from agent.scheduler import extract_slots, filter_matching_slots, find_available_slots
from agent.slot_selection import format_slot_options
from agent.slot_handoff import (
    appointment_has_passed,
    build_find_slots_args,
    scheduling_signature,
    slot_matches_offer,
)
from agent.state import AgentState
from agent.times import ARABIC_HOURS, clocks_within_clinic_hours, normalize_time
from core.contracts import ClinicGateway

_EARLIER = re.compile(
    r"(?:(?:is there |are there |do you have |any |anything |appointments? |slots? )*"
    r"(?:before|earlier than)\s+|"
    r"(?:(?:في|هل في|هل يوجد)\s+)?(?:(?:مواعيد|موعد)\s+)?(?:قبل|[أا]بكر من)\s+)"
    r"(?P<reference>.+)", re.I,
)
_REFERENCE = re.compile(
    r"(?:that(?: time| appointment| slot)?|the (?:time|appointment|slot)|"
    r"(?:هذا |هال)?الموعد|هالموعد|هذا الوقت|هالوقت)", re.I,
)
_CLOCK = re.compile(
    r"(?:(?:الساعة|الساعه)\s+)?(?:\d{1,2}(?::\d{2})?|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    + "|".join(map(re.escape, ARABIC_HOURS))
    + r")(?:\s*(?:[ap]\.?\s*m\.?|صباحا?|مساءا?|العصر))?", re.I,
)


def earlier_reference(message: str) -> str | None:
    """Recognize a time-only refinement and return its literal reference."""
    message = re.sub(r"[\u064b-\u065f\u0670]", "", message)
    match = _EARLIER.fullmatch(" ".join(message.split()).rstrip(".!?؟"))
    if match:
        reference = match["reference"]
        if _REFERENCE.fullmatch(reference) or _CLOCK.fullmatch(reference):
            return reference
    return None


def active_earlier_reference(state: AgentState) -> str | None:
    reference = earlier_reference(state.get("current_message", ""))
    if reference and (_REFERENCE.fullmatch(reference) or
                      (state.get("service_id") and state.get("requested_date"))):
        return reference
    return None


def _reference_clocks(reference: str) -> list[str] | None:
    if _REFERENCE.fullmatch(reference):
        return None
    time = normalize_time(reference)["requested_time"]
    if time:
        return [time]
    return [
        clock for period in ("AM", "PM")
        if (clock := normalize_time(f"{reference} {period}")["requested_time"])
    ]


async def search_earlier(state: AgentState, gateway: ClinicGateway, reference: str) -> dict:
    offers = [slot for slot in state.get("proposed_slots", []) if slot_matches_offer(state, slot)]
    clocks = _reference_clocks(reference)
    matching = [slot for slot in offers if clocks is None or slot["start"] in clocks]
    selected = [slot for slot in matching if slot["slot_id"] == state.get("selected_slot_id")]
    references = {(slot["date"], slot["start"]) for slot in selected or matching}

    # An explicit clock gives its own cutoff, but the appointment date still comes from context.
    if clocks and len(clocks) == 1 and not references:
        days = {slot["date"] for slot in offers} or {state.get("requested_date")}
        references = {(day, clocks[0]) for day in days if day}
    if len(references) != 1 or not state.get("service_id"):
        return {"pending_action": None,
                "stage": "awaiting_slot_selection" if offers else "awaiting_time_clarification",
                "reply": say(state, "Which appointment or time should I search before?",
                             "قبل أي موعد أو ساعة بدك أدور؟")}

    day, cutoff = references.pop()
    updates = {"requested_date": day, "time_kind": "window", "requested_time": None,
               "time_text": None, "time_window": None, "time_outside_hours": False, "time_before": cutoff,
               "time_change_pending": False, "time_change_clocks": [],
               "pending_action": None, "selected_slot_id": None, "selection_candidates": [],
               "proposed_slots": [], "offer_context": None}
    if day != state.get("requested_date"):
        updates["date_text"] = day
    search = {**state, **updates, "stage": "needs_slot_lookup"}
    try:
        clinic = await gateway.call_read("clinic_info", {})
        if clinic.get("ok") is not True:
            raise ValueError("Clinic calendar unavailable")
        updates.update(clinic_today=clinic.get("today") or state.get("clinic_today"),
                       clinic_calendar={**(state.get("clinic_calendar") or {}), **clinic})
        search.update(updates)
        after = search.get("time_after")
        slots = []
        if cutoff != "00:00" and (not after or after < cutoff) and not appointment_has_passed(search, day, cutoff):
            result = await find_available_slots(gateway, build_find_slots_args(search, limit=5))
            slots = [
                slot for slot in filter_matching_slots(search, extract_slots(result))
                if clocks_within_clinic_hours(search, [slot["start"]]) != []
            ]
    except Exception:
        return {"pending_action": None, "reply": say(
            state, "I couldn't check earlier appointments. Please try again.",
            "ما قدرت أتحقق من المواعيد الأبكر. جرّب مرة تانية، لو سمحت.")}

    if not slots:
        return {**updates, "stage": "no_slots", "reply": say(
            state, "There are no matching earlier appointments on that date.",
            "ما في مواعيد أبكر بتطابق طلبك بهاليوم.")}

    signature = scheduling_signature(search)
    return {**updates, "stage": "slots_proposed", "proposed_slots": slots,
            "offer_context": {"request": signature, "search": signature,
                              "slot_ids": [slot["slot_id"] for slot in slots]},
            "reply": format_slot_options(slots, search)}
