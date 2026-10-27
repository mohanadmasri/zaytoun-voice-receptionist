"""Zaytoun Family Clinic: the SIMULATED system of record (FastAPI).

The clinic is fictional and every record is simulated. State lives in memory and
is rebuilt from services/data/*.json at start-up and on POST /admin/reset.

Run:   uvicorn services.clinic_api:app --port 8101
Docs:  http://localhost:8101/docs
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
try:
    from dotenv import load_dotenv

    load_dotenv(HERE.parent / ".env")
except ImportError:  # python-dotenv is optional for the service
    pass

API_KEY = os.getenv("CLINIC_API_KEY") or "dev-key-change-me"
FAULT_TARGETS = ("availability", "create_booking", "cancel_booking", "get_patient")
FAULT_MODES = ("error_500", "error_503", "timeout", "slow", "slot_taken")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
GRID = 15
log = logging.getLogger("uvicorn.error")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def to_min(value: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ApiError(400, "invalid_request", f"'{value}' is not a time. Use HH:MM, 24-hour clock (e.g. 16:30).")
    return int(m.group(1)) * 60 + int(m.group(2) or 0)


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ApiError(400, "invalid_request", f"{field} '{value}' is not a date. Use YYYY-MM-DD.") from None


def weekday(d: date) -> str:
    return WEEKDAYS[d.weekday()]


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm_phone(raw: str) -> str:
    """Accepts 0599000111, 0599 000 111, +970 599 000 111, Arabic-Indic digits."""
    s = re.sub(r"[\s\-.()/]", "", str(raw or "").translate(ARABIC_DIGITS))
    s = s[1:] if s.startswith("+") else s
    s = s[2:] if s.startswith("00") else s
    for cc in ("970", "972"):
        if s.startswith(cc) and len(s) == 12:
            s = "0" + s[3:]
    if not re.fullmatch(r"0\d{8,9}", s):
        raise ApiError(400, "invalid_phone", f"'{raw}' is not a valid phone number. Use a local number such as 0599000111.")
    return s


def clinic_now(tz: ZoneInfo) -> datetime:
    """CLINIC_TODAY freezes the clock at CLINIC_TIME (default 08:00) on that day; empty = real time."""
    fixed = (os.getenv("CLINIC_TODAY") or "").strip()
    if fixed:
        try:
            d = date.fromisoformat(fixed)
        except ValueError:
            raise RuntimeError(f"CLINIC_TODAY='{fixed}' is not a date (YYYY-MM-DD).") from None
        t = to_min(os.getenv("CLINIC_TIME") or "08:00")
        return datetime(d.year, d.month, d.day, t // 60, t % 60, tzinfo=tz)
    return datetime.now(tz).replace(second=0, microsecond=0)


class Store:
    """All simulated state: reference data, bookings, patients, idempotency, faults, log."""

    def __init__(self) -> None:
        self.log: deque = deque(maxlen=300)
        self.reset()

    # ---------------------------------------------------------------- clock
    def now(self) -> datetime:
        return clinic_now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def last_bookable_day(self) -> date:
        return self.today() + timedelta(days=self.policy["booking_window_days"])

    def hours(self, d: date) -> tuple[int, int] | None:
        h = self.clinic["opening_hours"].get(weekday(d))
        return (to_min(h["open"]), to_min(h["close"])) if h else None

    # ---------------------------------------------------------------- reset / seed
    def reset(self) -> dict:
        self.clinic = load("clinic.json")
        self.services = {s["id"]: s for s in load("services.json")}
        self.providers = {p["id"]: p for p in load("providers.json")}
        self.patients = {p["phone"]: p for p in load("patients.json")}
        self.policy = self.clinic["policies"]
        self.tz = ZoneInfo(self.clinic["timezone"])
        self.bookings: dict[str, dict] = {}
        self.index: dict[tuple[str, str], list[dict]] = {}
        self.touched: list[str] = []
        self.created: list[str] = []
        self.next_id = 1001
        self.race_seq = 9000
        self.idem: dict[tuple[str, str], dict] = {}
        self.faults: dict[str, dict] = {}
        self.log.clear()
        self.engineered = self._seed(load("schedule_seed.json"))
        return self.summary()

    def summary(self) -> dict:
        today = self.today()
        open_min = booked = 0
        for k in range(self.policy["booking_window_days"] + 1):
            d = today + timedelta(days=k)
            hrs = self.hours(d)
            for p in self.providers.values():
                if hrs and weekday(d) in p["working_days"]:
                    open_min += hrs[1] - hrs[0]
                    booked += sum(b["duration_min"] for b in self.index.get((p["id"], d.isoformat()), []))
        return {"status": "reset", "today": today.isoformat(), "now": self.now().isoformat(),
                "weekday": weekday(today).capitalize(), "seed_bookings": len(self.bookings),
                "occupancy": round(booked / open_min, 3) if open_min else 0.0,
                "patients": len(self.patients), "engineered": self.engineered}

    def _resolve_day(self, ref: str) -> date:
        today = self.today()
        if ref == "today":
            return today
        m = re.fullmatch(r"first_(\w+)", ref)
        if not m or m.group(1) not in WEEKDAYS:
            raise RuntimeError(f"schedule_seed.json: bad day reference '{ref}'")
        delta = (WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        return today + timedelta(days=delta)

    def _free(self, pid: str, d: date, start: int, end: int) -> bool:
        return all(to_min(b["end"]) <= start or to_min(b["start"]) >= end
                   for b in self.index.get((pid, d.isoformat()), []))

    def _fits_day(self, pid: str, d: date, start: int, dur: int) -> bool:
        hrs = self.hours(d)
        return bool(hrs) and weekday(d) in self.providers[pid]["working_days"] and hrs[0] <= start and start + dur <= hrs[1]

    def _seed(self, rules: dict) -> list[dict]:
        seq = iter(range(4, 1000))
        engineered: list[dict] = []
        protected: list[tuple[str, date, int, int]] = []

        def place(bid: str, d: date, start: int, sid: str, pid: str, patient: dict) -> None:
            if not self._free(pid, d, start, start + self.services[sid]["duration_min"]):
                raise RuntimeError(f"schedule_seed.json: overlapping seed booking {bid} on {d} {hhmm(start)}")
            self._add(self._record(bid, d, start, self.services[sid], self.providers[pid], dict(patient), None, "seed"))

        for pb in rules["patient_bookings"]:
            start, sid, pid = to_min(pb["start"]), pb["service_id"], pb["provider_id"]
            first = self._resolve_day(pb["day"])
            d = next((first + timedelta(days=k) for k in range(8)
                      if self._fits_day(pid, first + timedelta(days=k), start, self.services[sid]["duration_min"])), None)
            if d is None:
                raise RuntimeError(f"schedule_seed.json: cannot place {pb['booking_id']}")
            place(pb["booking_id"], d, start, sid, pid, pb["patient"])
            engineered.append({"id": pb["booking_id"], "date": d.isoformat(), "start": pb["start"], "purpose": pb["purpose"]})

        seed_patient = rules.get("seed_patient") or {"name": "Other patient", "phone": None}
        for blk in rules["blocks"]:
            d, pid = self._resolve_day(blk["day"]), blk["provider_id"]
            hrs = self.hours(d)
            if not hrs or weekday(d) not in self.providers[pid]["working_days"]:
                continue
            a, b = max(to_min(blk["window"][0]), hrs[0]), min(to_min(blk["window"][1]), hrs[1])
            protected.append((pid, d, a, b))
            if blk.get("fill"):
                services, t, i = self.providers[pid]["service_ids"], a, 0
                while t < b:
                    for k in range(len(services)):
                        sid = services[(i + k) % len(services)]
                        dur = self.services[sid]["duration_min"]
                        if t + dur <= b and self._free(pid, d, t, t + dur):
                            place(f"BK-{next(seq):04d}", d, t, sid, pid, seed_patient)
                            t, i = -(-(t + dur) // GRID) * GRID - GRID, i + 1
                            break
                    t += GRID
            else:
                for start, sid in blk["bookings"]:
                    place(f"BK-{next(seq):04d}", d, to_min(start), sid, pid, seed_patient)
            engineered.append({"id": blk["id"], "date": d.isoformat(), "weekday": weekday(d).capitalize(),
                               "provider_id": pid, "purpose": blk["purpose"]})

        today, p = self.today(), rules["booking_probability"]
        for k in range(rules["horizon_days"] + 1):
            d = today + timedelta(days=k)
            hrs = self.hours(d)
            if not hrs:
                continue
            for pid in sorted(self.providers):
                prov = self.providers[pid]
                if weekday(d) not in prov["working_days"]:
                    continue
                rng = random.Random(f"{rules['random_seed']}|{d.isoformat()}|{pid}")
                t = hrs[0]
                while t < hrs[1]:
                    sid = rng.choice(prov["service_ids"])
                    end = t + self.services[sid]["duration_min"]
                    blocked = any(q == pid and qd == d and t < qb and end > qa for q, qd, qa, qb in protected)
                    if rng.random() < p and end <= hrs[1] and not blocked and self._free(pid, d, t, end):
                        place(f"BK-{next(seq):04d}", d, t, sid, pid, seed_patient)
                        t = -(-end // GRID) * GRID
                    else:
                        t += GRID
        return engineered

    # ---------------------------------------------------------------- records
    def _record(self, bid: str, d: date, start: int, svc: dict, prov: dict, patient: dict,
                notes: str | None, source: str) -> dict:
        dur = svc["duration_min"]
        return {
            "booking_id": bid, "status": "confirmed",
            "slot_id": f"SL-{d.strftime('%Y%m%d')}-{hhmm(start).replace(':', '')}-{prov['id']}",
            "service_id": svc["id"], "service_name": svc["name_en"], "service_name_ar": svc["name_ar"],
            "provider_id": prov["id"], "provider_name": prov["name"],
            "date": d.isoformat(), "weekday": weekday(d).capitalize(),
            "start": hhmm(start), "end": hhmm(start + dur), "duration_min": dur,
            "start_at": datetime(d.year, d.month, d.day, start // 60, start % 60, tzinfo=self.tz).isoformat(),
            "patient": patient, "notes": notes, "source": source,
            "created_at": self.now().isoformat(), "cancelled_at": None, "cancel_reason": None,
        }

    def _add(self, rec: dict) -> None:
        self.bookings[rec["booking_id"]] = rec
        self.index.setdefault((rec["provider_id"], rec["date"]), []).append(rec)

    def slot_view(self, svc: dict, d: date, start: int, prov: dict) -> dict:
        return {"slot_id": f"SL-{d.strftime('%Y%m%d')}-{hhmm(start).replace(':', '')}-{prov['id']}",
                "date": d.isoformat(), "weekday": weekday(d).capitalize(), "start": hhmm(start),
                "end": hhmm(start + svc["duration_min"]), "provider_id": prov["id"], "provider_name": prov["name"]}

    def free_slots(self, svc: dict, d: date, pids: list[str], after: int | None, before: int | None) -> list[dict]:
        hrs, now, out = self.hours(d), self.now(), []
        if not hrs:
            return out
        for pid in pids:
            if weekday(d) not in self.providers[pid]["working_days"]:
                continue
            t = hrs[0]
            while t + svc["duration_min"] <= hrs[1]:
                in_window = (after is None or t >= after) and (before is None or t < before)
                future = d > now.date() or (d == now.date() and t >= now.hour * 60 + now.minute)
                if in_window and future and self._free(pid, d, t, t + svc["duration_min"]):
                    out.append(self.slot_view(svc, d, t, self.providers[pid]))
                t += GRID
        return sorted(out, key=lambda s: (s["date"], s["start"], s["provider_id"]))

    def upcoming_for(self, phone: str) -> list[dict]:
        now = self.now()
        rows = [b for b in self.bookings.values() if b["status"] == "confirmed"
                and (b["patient"] or {}).get("phone") == phone and datetime.fromisoformat(b["start_at"]) >= now]
        return sorted(rows, key=lambda b: b["start_at"])

    # ---------------------------------------------------------------- faults
    async def apply_fault(self, target: str) -> str | None:
        """Consume one fault for `target`. Raises for error modes; returns "slot_taken" for that mode."""
        fault = self.faults.get(target)
        if not fault:
            return None
        fault["count"] -= 1
        if fault["count"] <= 0:
            del self.faults[target]
        mode = fault["mode"]
        if mode == "slot_taken":
            return mode
        if mode == "slow":
            await asyncio.sleep(3)
        elif mode == "timeout":
            await asyncio.sleep(15)
            raise ApiError(504, "gateway_timeout", "The clinic system timed out (simulated). Nothing was changed.")
        elif mode == "error_503":
            raise ApiError(503, "service_unavailable", "The clinic system is temporarily unavailable (simulated). Nothing was changed. Try again later.")
        else:
            raise ApiError(500, "internal_error", "The clinic system had an internal error (simulated). Nothing was changed.")
        return None

    def book_for_someone_else(self, d: date, start: int, svc: dict, prov: dict) -> None:
        """Fault mode slot_taken: another patient takes this slot just before the request (a race)."""
        if self._free(prov["id"], d, start, start + svc["duration_min"]):
            self.race_seq += 1
            self._add(self._record(f"BK-{self.race_seq}", d, start, svc, prov, {"name": "Other patient", "phone": None},
                                   None, "other_channel"))


STORE = Store()
app = FastAPI(title="Zaytoun Family Clinic API (simulated)", version="1.0",
              description="Simulated system of record for a fictional clinic. Business endpoints need the X-API-Key header. "
                          "/admin/* endpoints are TEST-ONLY: your agent must never call them.")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_key(key: str | None = Depends(api_key_header)) -> None:
    if not key or not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise ApiError(401, "unauthorized", "Missing or wrong X-API-Key header (it must match CLINIC_API_KEY).")


def error_body(code: str, message: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    headers = {"Retry-After": "2"} if exc.status == 503 else None
    return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, **exc.extra), headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query", "path"))
        parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
    return JSONResponse(status_code=400, content=error_body("invalid_request", "; ".join(parts) or "Invalid request."))


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, f"http_{exc.status_code}")
    return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)))


OPERATIONS = [("GET", r"/api/clinic", "clinic_info"), ("GET", r"/api/services", "list_services"),
              ("GET", r"/api/providers", "list_providers"), ("GET", r"/api/availability", "availability"),
              ("POST", r"/api/bookings", "create_booking"), ("GET", r"/api/bookings/[^/]+", "get_booking"),
              ("DELETE", r"/api/bookings/[^/]+", "cancel_booking"), ("GET", r"/api/patients/[^/]+", "get_patient"),
              ("PUT", r"/api/patients/[^/]+", "save_patient"), ("GET", r"/health", "health")]


def operation_for(method: str, path: str) -> str:
    for m, pattern, name in OPERATIONS:
        if m == method and re.fullmatch(pattern, path):
            return name
    return "admin_" + path.split("/")[2] if path.startswith("/admin/") else "other"


@app.middleware("http")
async def request_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if path != "/admin/log":
        STORE.log.append({"ts": datetime.now(STORE.tz).isoformat(timespec="seconds"), "method": request.method,
                          "path": path + (f"?{request.url.query}" if request.url.query else ""),
                          "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                          "operation": operation_for(request.method, path),
                          "idempotency_key": request.headers.get("idempotency-key")})
    return response


# ======================================================================= health
@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "service": "clinic-api"}


# ======================================================================= business API
@app.get("/api/clinic", tags=["clinic"], dependencies=[Depends(require_key)])
def get_clinic() -> dict:
    """Clinic details, opening hours, policies, providers, and the clinic's current date and time."""
    now, info = STORE.now(), {k: v for k, v in STORE.clinic.items() if not k.startswith("_")}
    info.update({"today": now.date().isoformat(), "weekday": weekday(now.date()).capitalize(),
                 "now": now.isoformat(), "last_bookable_day": STORE.last_bookable_day().isoformat(),
                 "providers": [{k: p[k] for k in ("id", "name", "name_ar", "role", "service_ids", "working_days", "languages")}
                               for p in STORE.providers.values()]})
    return info


@app.get("/api/services", tags=["clinic"], dependencies=[Depends(require_key)])
def list_services() -> dict:
    out = []
    for s in STORE.services.values():
        out.append({**s, "providers": [{"id": pid, "name": STORE.providers[pid]["name"],
                                        "working_days": STORE.providers[pid]["working_days"]} for pid in s["provider_ids"]]})
    return {"services": out}


@app.get("/api/providers", tags=["clinic"], dependencies=[Depends(require_key)])
def list_providers() -> dict:
    return {"providers": list(STORE.providers.values())}


def get_service(service_id: str) -> dict:
    svc = STORE.services.get((service_id or "").strip())
    if not svc:
        raise ApiError(404, "unknown_service", f"Unknown service_id '{service_id}'. Valid ids: {', '.join(STORE.services)}.")
    return svc


def check_provider(svc: dict, provider_id: str) -> dict:
    prov = STORE.providers.get(provider_id)
    if not prov:
        raise ApiError(404, "unknown_provider", f"Unknown provider_id '{provider_id}'. Valid ids: {', '.join(STORE.providers)}.")
    if svc["id"] not in prov["service_ids"]:
        names = ", ".join(STORE.providers[p]["name"] for p in svc["provider_ids"])
        raise ApiError(400, "invalid_request", f"{prov['name']} does not provide {svc['name_en']}. It is provided by: {names}.")
    return prov


@app.get("/api/availability", tags=["clinic"], dependencies=[Depends(require_key)])
async def availability(service_id: str = Query(..., description="e.g. dental_checkup"),
                       date_from: str | None = Query(None, description="YYYY-MM-DD, default today"),
                       date_to: str | None = Query(None, description="YYYY-MM-DD, default date_from"),
                       after: str | None = Query(None, description="HH:MM: slots starting at or after"),
                       before: str | None = Query(None, description="HH:MM: slots starting before"),
                       provider_id: str | None = None,
                       limit: int = Query(8, ge=1, le=50)) -> dict:
    """Free slots for one service. Every slot respects opening hours, provider days and existing bookings."""
    await STORE.apply_fault("availability")
    svc = get_service(service_id)
    pids = [check_provider(svc, provider_id)["id"]] if provider_id else list(svc["provider_ids"])
    today, last = STORE.today(), STORE.last_bookable_day()
    d1 = parse_date(date_from, "date_from") if date_from else today
    d2 = parse_date(date_to, "date_to") if date_to else d1
    a = to_min(after) if after else None
    b = to_min(before) if before else None
    if d2 < d1:
        raise ApiError(400, "invalid_request", "date_to is before date_from.")
    if d2 < today:
        raise ApiError(422, "policy_violation", f"Those dates are in the past. Today is {today.isoformat()} ({weekday(today).capitalize()}).")
    if d1 > last:
        raise ApiError(422, "policy_violation", f"Bookings open {STORE.policy['booking_window_days']} days ahead. The last bookable day is {last.isoformat()}.")
    notes: list[str] = []
    if d1 < today:
        notes.append(f"Dates before today ({today.isoformat()}) were skipped.")
        d1 = today
    if d2 > last:
        notes.append(f"Dates after {last.isoformat()} are outside the {STORE.policy['booking_window_days']}-day booking window and were skipped.")
        d2 = last
    slots: list[dict] = []
    d = d1
    while d <= d2:
        day_slots = STORE.free_slots(svc, d, pids, a, b)
        hrs, label = STORE.hours(d), f"{d.isoformat()} ({weekday(d).capitalize()})"
        if not hrs:
            notes.append(f"{label}: the clinic is closed.")
        elif not day_slots:
            working = [p for p in pids if weekday(d) in STORE.providers[p]["working_days"]]
            if not working:
                who = STORE.providers[pids[0]]["name"] + " does" if len(pids) == 1 else "No provider for this service does"
                notes.append(f"{label}: {who} not work that day.")
            elif a is not None and a > hrs[1] - svc["duration_min"]:
                notes.append(f"{label}: the clinic is open {hhmm(hrs[0])}-{hhmm(hrs[1])}; no {svc['name_en']} can start at {after} or later.")
            else:
                notes.append(f"{label}: no free {svc['name_en']} slots match (fully booked for these filters).")
        slots.extend(day_slots)
        d += timedelta(days=1)
    return {"service_id": svc["id"], "service_name": svc["name_en"], "service_name_ar": svc["name_ar"],
            "duration_min": svc["duration_min"], "date_from": d1.isoformat(), "date_to": d2.isoformat(),
            "after": after, "before": before, "provider_id": provider_id, "slots": slots[:limit],
            "count": min(len(slots), limit), "total_matching": len(slots), "truncated": len(slots) > limit, "notes": notes}


class PatientIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    phone: str
    guardian_name: str | None = Field(None, max_length=80, description="Required when the patient is under 16")
    age: int | None = Field(None, ge=0, le=120)


class BookingIn(BaseModel):
    slot_id: str
    service_id: str
    patient: PatientIn
    notes: str | None = Field(None, max_length=500)


def fingerprint(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(op: str, key: str | None, body: Any) -> JSONResponse | None:
    if not key:
        return None
    if len(key) > 200:
        raise ApiError(400, "invalid_request", "Idempotency-Key is longer than 200 characters.")
    hit = STORE.idem.get((op, key))
    if not hit:
        return None
    if hit["fingerprint"] != fingerprint(body):
        raise ApiError(409, "idempotency_key_reused", "This Idempotency-Key was already used with a different request body.")
    return JSONResponse(status_code=hit["status"], content=hit["body"], headers={"Idempotent-Replayed": "true"})


@app.post("/api/bookings", status_code=201, tags=["clinic"], dependencies=[Depends(require_key)])
async def create_booking(body: BookingIn, request: Request):
    """Book one slot. Send an Idempotency-Key header: the same key and body never book twice."""
    race = await STORE.apply_fault("create_booking")
    key = request.headers.get("idempotency-key")
    raw = body.model_dump()
    replayed = replay("create_booking", key, raw)
    if replayed:
        return replayed
    svc = get_service(body.service_id)
    m = re.fullmatch(r"SL-(\d{8})-(\d{4})-([a-z_]+)", body.slot_id.strip())
    if not m:
        raise ApiError(400, "invalid_slot", f"'{body.slot_id}' is not a slot id. Use a slot_id returned by /api/availability.")
    try:
        d = datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        raise ApiError(400, "invalid_slot", f"'{body.slot_id}' has an invalid date.") from None
    start = to_min(m.group(2)[:2] + ":" + m.group(2)[2:])
    prov = check_provider(svc, m.group(3))
    now, last, dur = STORE.now(), STORE.last_bookable_day(), svc["duration_min"]
    hrs = STORE.hours(d)
    label = f"{weekday(d).capitalize()} {d.isoformat()}"
    if d < now.date() or (d == now.date() and start < now.hour * 60 + now.minute):
        raise ApiError(422, "policy_violation", f"{label} {hhmm(start)} is in the past.")
    if d > last:
        raise ApiError(422, "policy_violation", f"Bookings open {STORE.policy['booking_window_days']} days ahead. The last bookable day is {last.isoformat()}.")
    if not hrs:
        raise ApiError(422, "policy_violation", f"The clinic is closed on {weekday(d).capitalize()}s.")
    if weekday(d) not in prov["working_days"]:
        raise ApiError(422, "policy_violation", f"{prov['name']} does not work on {weekday(d).capitalize()}s.")
    if start % GRID:
        raise ApiError(400, "invalid_slot", "Appointments start on a 15-minute grid (:00, :15, :30, :45).")
    if start < hrs[0] or start + dur > hrs[1]:
        raise ApiError(422, "policy_violation", f"On {weekday(d).capitalize()}s the clinic is open {hhmm(hrs[0])}-{hhmm(hrs[1])}. "
                                                f"A {dur}-minute {svc['name_en']} must start between {hhmm(hrs[0])} and {hhmm(hrs[1] - dur)}.")
    phone = norm_phone(body.patient.phone)
    name = body.patient.name.strip()
    guardian = (body.patient.guardian_name or "").strip()
    min_age = STORE.policy["guardian_required_under_age"]
    if (svc["id"] == "pediatrics_visit" or (body.patient.age is not None and body.patient.age < min_age)) and len(guardian) < 2:
        raise ApiError(422, "policy_violation", f"A patient under {min_age} needs a parent or guardian: send patient.guardian_name. "
                                                "Every pediatrics visit needs it.")
    if len(STORE.upcoming_for(phone)) >= STORE.policy["max_upcoming_bookings_per_patient"]:
        raise ApiError(422, "policy_violation", f"This phone number already has {STORE.policy['max_upcoming_bookings_per_patient']} "
                                                "upcoming appointments, the maximum per patient.")
    if race:
        STORE.book_for_someone_else(d, start, svc, prov)
    if not STORE._free(prov["id"], d, start, start + dur):
        raise ApiError(409, "slot_taken", f"{prov['name']} is not free for a {dur}-minute {svc['name_en']} at {hhmm(start)} on {label}. "
                                          "Search /api/availability again for free times.")
    bid = f"BK-{STORE.next_id}"
    STORE.next_id += 1
    patient = {"name": name, "phone": phone}
    if guardian:
        patient["guardian_name"] = guardian
    if body.patient.age is not None:
        patient["age"] = body.patient.age
    rec = STORE._record(bid, d, start, svc, prov, patient, body.notes, "api")
    STORE._add(rec)
    STORE.created.append(bid)
    STORE.touched.append(bid)
    new_patient = phone not in STORE.patients
    if new_patient:
        STORE.patients[phone] = {"phone": phone, "name": guardian or name, "language": None, "preferred_provider_id": None,
                                 "notes": f"Registered automatically at first booking ({bid}).", "last_visit": None}
    msg = f"Booked {svc['name_en']} on {label} at {hhmm(start)} with {prov['name']}."
    if new_patient:
        msg += f" New patient: please arrive {STORE.policy['new_patient_arrive_early_minutes']} minutes early."
    out = {**rec, "new_patient": new_patient, "message": msg}
    if key:
        STORE.idem[("create_booking", key)] = {"fingerprint": fingerprint(raw), "status": 201, "body": out}
    return JSONResponse(status_code=201, content=out)


def find_booking(booking_id: str) -> dict:
    rec = STORE.bookings.get((booking_id or "").strip().upper())
    if not rec:
        raise ApiError(404, "unknown_booking", f"No booking with id '{booking_id}'.")
    return rec


@app.get("/api/bookings/{booking_id}", tags=["clinic"], dependencies=[Depends(require_key)])
def get_booking(booking_id: str) -> dict:
    return find_booking(booking_id)


@app.delete("/api/bookings/{booking_id}", tags=["clinic"], dependencies=[Depends(require_key)])
async def cancel_booking(booking_id: str, request: Request, reason: str | None = Query(None, max_length=300)):
    """Cancel a booking. Refused (409) less than 24 hours before the start."""
    await STORE.apply_fault("cancel_booking")
    key = request.headers.get("idempotency-key")
    replayed = replay("cancel_booking", key, {"booking_id": booking_id, "reason": reason})
    if replayed:
        return replayed
    rec = find_booking(booking_id)
    if rec["status"] == "cancelled":
        raise ApiError(409, "already_cancelled", f"Booking {rec['booking_id']} is already cancelled.")
    start = datetime.fromisoformat(rec["start_at"])
    hours_left = (start - STORE.now()).total_seconds() / 3600
    min_h = STORE.policy["cancellation_min_hours"]
    if hours_left < min_h:
        when = "has already started or passed" if hours_left <= 0 else f"starts in {hours_left:.1f} hours"
        raise ApiError(409, "too_late_to_cancel", f"Booking {rec['booking_id']} {when} ({rec['date']} {rec['start']}). "
                                                  f"Cancellations must be made at least {min_h} hours before. "
                                                  f"The patient can call the clinic on {STORE.clinic['phone']}.")
    STORE.index[(rec["provider_id"], rec["date"])].remove(rec)
    rec.update({"status": "cancelled", "cancelled_at": STORE.now().isoformat(), "cancel_reason": reason})
    if rec["booking_id"] not in STORE.touched:
        STORE.touched.append(rec["booking_id"])
    out = {**rec, "message": f"Cancelled {rec['service_name']} on {rec['weekday']} {rec['date']} at {rec['start']}."}
    if key:
        STORE.idem[("cancel_booking", key)] = {"fingerprint": fingerprint({"booking_id": booking_id, "reason": reason}),
                                               "status": 200, "body": out}
    return out


def patient_view(p: dict) -> dict:
    prov = STORE.providers.get(p.get("preferred_provider_id") or "")
    return {**p, "preferred_provider_name": prov["name"] if prov else None,
            "upcoming_bookings": [{k: b[k] for k in ("booking_id", "service_id", "service_name", "provider_id", "provider_name",
                                                     "date", "weekday", "start", "end", "status")}
                                  | {"patient_name": b["patient"]["name"]} for b in STORE.upcoming_for(p["phone"])]}


@app.get("/api/patients/{phone}", tags=["clinic"], dependencies=[Depends(require_key)])
async def get_patient(phone: str) -> dict:
    """A returning patient's record (preferences and upcoming bookings). 404 for an unknown phone."""
    await STORE.apply_fault("get_patient")
    p = STORE.patients.get(norm_phone(phone))
    if not p:
        raise ApiError(404, "unknown_patient", "No patient with this phone number. Treat the caller as a new patient.")
    return patient_view(p)


class PatientUpsert(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    language: str | None = Field(None, description="en or ar")
    preferred_provider_id: str | None = None
    notes: str | None = Field(None, max_length=500)


@app.put("/api/patients/{phone}", tags=["clinic"], dependencies=[Depends(require_key)])
def save_patient(phone: str, body: PatientUpsert):
    """Create or update a patient record. Fields you leave out (null) keep their current value."""
    phone = norm_phone(phone)
    if body.language is not None and body.language not in ("en", "ar"):
        raise ApiError(400, "invalid_request", "language must be 'en' or 'ar'.")
    if body.preferred_provider_id is not None and body.preferred_provider_id not in STORE.providers:
        raise ApiError(400, "invalid_request", f"Unknown provider_id '{body.preferred_provider_id}'. Valid ids: {', '.join(STORE.providers)}.")
    created = phone not in STORE.patients
    p = STORE.patients.setdefault(phone, {"phone": phone, "name": body.name.strip(), "language": None,
                                          "preferred_provider_id": None, "notes": None, "last_visit": None})
    p["name"] = body.name.strip()
    for field in ("language", "preferred_provider_id", "notes"):
        if getattr(body, field) is not None:
            p[field] = getattr(body, field)
    return JSONResponse(status_code=201 if created else 200, content={**patient_view(p), "created": created})


# ======================================================================= admin (TEST-ONLY)
class FaultIn(BaseModel):
    target: str
    mode: str
    count: int = Field(1, ge=1, le=100)


@app.post("/admin/reset", tags=["admin (test-only)"])
def admin_reset() -> dict:
    """Restore the seed state, clear faults, idempotency keys and the log."""
    return STORE.reset()


@app.post("/admin/faults", tags=["admin (test-only)"])
def admin_add_fault(body: FaultIn) -> dict:
    if body.target not in FAULT_TARGETS:
        raise ApiError(400, "invalid_request", f"target must be one of: {', '.join(FAULT_TARGETS)}.")
    if body.mode not in FAULT_MODES:
        raise ApiError(400, "invalid_request", f"mode must be one of: {', '.join(FAULT_MODES)}.")
    if body.mode == "slot_taken" and body.target != "create_booking":
        raise ApiError(400, "invalid_request", "mode slot_taken only works with target create_booking.")
    STORE.faults[body.target] = {"mode": body.mode, "count": body.count}
    return {"faults": [{"target": t, **f} for t, f in STORE.faults.items()]}


@app.get("/admin/faults", tags=["admin (test-only)"])
def admin_list_faults() -> dict:
    return {"faults": [{"target": t, **f} for t, f in STORE.faults.items()]}


@app.delete("/admin/faults", tags=["admin (test-only)"])
def admin_clear_faults() -> dict:
    STORE.faults.clear()
    return {"faults": []}


@app.get("/admin/log", tags=["admin (test-only)"])
def admin_log(limit: int = Query(100, ge=1, le=300)) -> list:
    return list(STORE.log)[-limit:]


@app.get("/admin/bookings", tags=["admin (test-only)"])
def admin_bookings(all: int = 0, changed: int = 0) -> list:
    """Default: bookings CREATED since the last reset. ?changed=1: created or cancelled since the reset. ?all=1: everything (seed, new, other channel)."""
    if all:
        return sorted(STORE.bookings.values(), key=lambda b: b["booking_id"])
    return [STORE.bookings[i] for i in (STORE.touched if changed else STORE.created)]


@app.get("/admin/patients", tags=["admin (test-only)"])
def admin_patients() -> list:
    return list(STORE.patients.values())


log.info("Zaytoun clinic API (simulated): today=%s, %d seed bookings, occupancy %.0f%%",
         STORE.today().isoformat(), len(STORE.bookings), STORE.summary()["occupancy"] * 100)
