
# scheduler.py
# This file searches for available clinic appointments.
# It uses the MCP Gateway to retrieve real appointment slots.
# It never creates or cancels bookings.

from agent.state import AgentState
from core.contracts import ClinicGateway


# Validate the patient request and search for available appointments.
async def schedule(
    state: AgentState,
    gateway: ClinicGateway
) -> dict:

    # Read the requested service and date from the conversation state.
    service_id = state.get("service_id")
    requested_date = state.get("requested_date")

    # Do not search if the requested service is missing.
    if not service_id:
        return {
            "stage": "needs_service_mapping",
            "reply": "The appointment service must be identified first.",
            "proposed_slots": [],
        }

    # Ask for a date if the patient has not provided one.
    if not requested_date:
        return {
            "stage": "awaiting_details",
            "reply": "Which date would you like to book?",
            "proposed_slots": [],
        }

    # Request real available slots through the MCP Gateway.
    result = await gateway.call_read(
        "find_slots",
        {
            "service_id": service_id,
            "date_from": requested_date,
            "date_to": requested_date,
            "limit": 5,
        },
    )

    # Handle a failed clinic request without inventing appointments.
    if not result.get("ok"):
        return {
            "stage": "scheduler_error",
            "reply": "Unable to search for appointments right now.",
            "proposed_slots": [],
        }

    # Extract the available slots returned by the clinic.
    slots = result.get("slots", [])

    # Inform the patient when no appointments are available.
    if not slots:
        return {
            "stage": "no_slots",
            "reply": "No appointments are available on this date.",
            "proposed_slots": [],
        }

    # Return the real slots for the conversation state.
    return {
        "stage": "slots_proposed",
        "reply": "Available appointments were found. Please select a slot.",
        "proposed_slots": slots,
    }
