"""MCP server (stdio) for the simulated Zaytoun Family Clinic.

It is an adapter in front of the clinic's REST API: every tool calls the API over
HTTP with the X-API-Key header, so the API and every MCP session share one state.

Run it the way an MCP client does (from the repo root):
    python -m services.clinic_mcp
Env: CLINIC_API_URL (default http://localhost:8101), CLINIC_API_KEY, CLINIC_HTTP_TIMEOUT (seconds, default 10).

Every tool returns a dict: {"ok": true, ...data} or {"ok": false, "status": <http code>, "error": {"code", "message", "retryable"}}.
Tool errors never raise, so one failed call does not kill the MCP session.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# mcp 1.28 with pydantic-settings 2.16 prints a harmless IncompleteFieldDefinitionWarning on start-up.
warnings.filterwarnings("ignore", message=r".*'lifespan' has an incomplete definition.*")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

API_URL = (os.getenv("CLINIC_API_URL") or "http://localhost:8101").rstrip("/")
API_KEY = os.getenv("CLINIC_API_KEY") or "dev-key-change-me"
TIMEOUT = float(os.getenv("CLINIC_HTTP_TIMEOUT") or 10)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

mcp = FastMCP(
    "zaytoun-clinic",
    instructions=("Tools for the simulated Zaytoun Family Clinic (fictional). Read tools: clinic_info, list_services, "
                  "find_slots, get_patient, get_booking. Write tools: save_patient, create_booking, cancel_booking. "
                  "Never book or cancel without the patient's explicit approval of that exact action."),
    log_level="WARNING",
)


async def _call(method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                headers: dict | None = None) -> dict[str, Any]:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=TIMEOUT) as client:
            r = await client.request(method, path, params=clean, json=body,
                                     headers={"X-API-Key": API_KEY, **(headers or {})})
    except httpx.TimeoutException:
        return {"ok": False, "status": None, "error": {
            "code": "timeout", "retryable": True,
            "message": f"The clinic system did not answer within {TIMEOUT:g} s. Treat the operation as NOT done. "
                       "You may retry later; reuse the same idempotency_key for a booking."}}
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "error": {
            "code": "unreachable", "retryable": True,
            "message": f"Cannot reach the clinic system at {API_URL} ({exc.__class__.__name__}). Is it running?"}}
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400:
        err = dict(data.get("error") or {}) if isinstance(data, dict) else {}
        err.setdefault("code", f"http_{r.status_code}")
        err.setdefault("message", str(data)[:300])
        err["retryable"] = r.status_code in (429, 500, 502, 503, 504)
        return {"ok": False, "status": r.status_code, "error": err}
    return {"ok": True, **data} if isinstance(data, dict) else {"ok": True, "items": data}


@mcp.tool(annotations=READ)
async def clinic_info() -> dict[str, Any]:
    """Get the clinic's details: name, address, phone, opening hours, policies, FAQ, providers,
    and TODAY's date, weekday and current time in the clinic's time zone (Asia/Jerusalem).

    Call it at the start of a conversation. Use its `today` and `weekday` to turn relative dates
    ("tomorrow", "next Tuesday", "this Friday") into YYYY-MM-DD. Never guess today's date.
    Use `policies` and `faq` to answer questions about hours, cancellations, guardians and emergencies.
    """
    return await _call("GET", "/api/clinic")


@mcp.tool(annotations=READ)
async def list_services() -> dict[str, Any]:
    """List the bookable services: service_id, English and Arabic names, duration in minutes,
    price in ILS (information only) and the providers who perform each service.

    Use it to map what the patient says ("a cleaning", "تنظيف أسنان", "see a GP") to a service_id
    before calling find_slots, and to map a provider's name ("Dr. Khoury") to a provider_id.
    """
    return await _call("GET", "/api/services")


@mcp.tool(annotations=READ)
async def find_slots(service_id: str, date_from: str, date_to: str, after: str | None = None,
                     before: str | None = None, provider_id: str | None = None, limit: int = 8) -> dict[str, Any]:
    """Find FREE appointment slots for one service between two dates (inclusive).

    Args:
        service_id: an id from list_services, e.g. "dental_checkup".
        date_from, date_to: YYYY-MM-DD (use the same date for one day). Resolve relative dates with clinic_info first.
        after: optional "HH:MM" (24-hour clinic time): keep slots starting at or after it ("after 4 PM" -> "16:00").
        before: optional "HH:MM": keep slots starting before it ("in the morning" -> before "12:00").
        provider_id: optional, only this provider (e.g. the patient's preferred provider).
        limit: maximum slots returned (1-50, earliest first).

    Use it before proposing ANY time: propose only slot_id values returned here. When the requested time
    is not in the result, search a window around it (e.g. after "15:00", before "19:00") and offer the nearest
    alternatives. Read `notes`: they explain closed days, opening hours and fully booked days.
    A slot can still be taken by someone else before you book; then create_booking returns slot_taken.
    """
    return await _call("GET", "/api/availability", params={
        "service_id": service_id, "date_from": date_from, "date_to": date_to, "after": after,
        "before": before, "provider_id": provider_id, "limit": limit})


@mcp.tool(annotations=READ)
async def get_patient(phone: str) -> dict[str, Any]:
    """Look up a returning patient by phone number: name, preferred language ("en"/"ar"),
    preferred provider, short notes, last visit and upcoming bookings (each with its booking_id).

    Use it as soon as the caller gives a phone number, to greet them by name, use their preferences,
    and find the booking they want to cancel or move. Returns ok=false with code "unknown_patient"
    for a new patient: that is normal, just continue as a new patient.
    """
    return await _call("GET", f"/api/patients/{quote(phone.strip(), safe='')}")


@mcp.tool(annotations=WRITE)
async def save_patient(phone: str, name: str, language: str | None = None,
                       preferred_provider_id: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Create or update the patient's record in the clinic system (memory across conversations).

    Args:
        phone: the patient's phone number (the record key).
        name: full name.
        language: "en" or "ar" (the language the patient prefers).
        preferred_provider_id: a provider id from list_services, e.g. "dr_khoury".
        notes: a short, NON-medical preference such as "prefers mornings". Never store symptoms or diagnoses.

    Use it when a new patient gives their details or a patient states a lasting preference.
    Fields left as null keep their current value.
    """
    return await _call("PUT", f"/api/patients/{quote(phone.strip(), safe='')}", body={
        "name": name, "language": language, "preferred_provider_id": preferred_provider_id, "notes": notes})


@mcp.tool(annotations=WRITE)
async def create_booking(slot_id: str, service_id: str, patient_name: str, patient_phone: str,
                         idempotency_key: str, guardian_name: str | None = None,
                         notes: str | None = None) -> dict[str, Any]:
    """WRITE: book one appointment slot in the clinic system.

    Call it ONLY after the patient explicitly approved this exact slot (service, day, time, provider)
    and these patient details. In a safe design your code calls it from the approval step
    (POST /api/approve), never the model on its own.

    Args:
        slot_id: a slot_id returned by find_slots, e.g. "SL-20261103-1630-dr_khoury".
        service_id: the same service used in find_slots.
        patient_name, patient_phone: who the appointment is for (for a child: the child's name, the guardian's phone).
        idempotency_key: a unique id for this approved action (use the action_id). Retrying with the
            same key never books twice; it returns the first result.
        guardian_name: REQUIRED for a patient under 16 and for every pediatrics_visit.
        notes: optional short non-medical note.

    Returns the booking with its real booking_id (BK-...). Errors: slot_taken (409, offer other slots),
    policy_violation (422, read the message to the patient), service_unavailable/timeout (nothing booked).
    """
    patient: dict[str, Any] = {"name": patient_name, "phone": patient_phone}
    if guardian_name:
        patient["guardian_name"] = guardian_name
    return await _call("POST", "/api/bookings", headers={"Idempotency-Key": idempotency_key}, body={
        "slot_id": slot_id, "service_id": service_id, "patient": patient, "notes": notes})


@mcp.tool(annotations=READ)
async def get_booking(booking_id: str) -> dict[str, Any]:
    """Read one booking by its id (e.g. "BK-1001"): service, provider, date, time, patient, status.

    Use it when the patient quotes a booking id, or to check a booking before proposing to cancel it.
    """
    return await _call("GET", f"/api/bookings/{quote(booking_id.strip(), safe='')}")


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_booking(booking_id: str, reason: str) -> dict[str, Any]:
    """WRITE: cancel one booking.

    Call it ONLY after the patient explicitly approved cancelling this exact booking (check it first with
    get_patient or get_booking, and make sure it belongs to the caller). Cancellations less than 24 hours
    before the start are refused with code "too_late_to_cancel": tell the patient to call the clinic.
    """
    return await _call("DELETE", f"/api/bookings/{quote(booking_id.strip(), safe='')}", params={"reason": reason})


if __name__ == "__main__":
    mcp.run()
