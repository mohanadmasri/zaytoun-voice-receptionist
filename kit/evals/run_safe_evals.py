# يشغل run_case وwrite_scorecard الأصليين دون تعديل معيار النجاح، بحدود LLM/عيادة وهمية.
# لا منافذ ولا شبكة ولا reset أو كتابة في العيادة العاملة؛ النتائج ليست تقييم LLM/STT حيًا.
import json
import sys
import time
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi.testclient import TestClient
from app.main import app, get_gateway
from agent.intake import IntakeResult
from core.session_store import session_store
from evals.run_evals import run_case, write_scorecard

ROOT = Path(__file__).resolve().parents[1]


# نظام سجل وهمي في RAM مع رفض الحجز المكرر وسياسات أيام العمل؛ لا يصل إلى ClinicGateway.
class EvalGateway:
    def __init__(self, fixture=None):
        self.fixture = fixture or {}
        self.patients = {}
        self.bookings = {}
        self.idempotency = {}
        self.writes = []
        self.clinic = json.loads((ROOT / "services/data/clinic.json").read_text(encoding="utf-8"))
        self.providers = json.loads((ROOT / "services/data/providers.json").read_text(encoding="utf-8"))
        self.services = json.loads((ROOT / "services/data/services.json").read_text(encoding="utf-8"))
        if self.fixture.get("cancellation"):
            self.bookings["BK-EXISTING"] = {"booking_id": "BK-EXISTING", "status": "confirmed", "service_id": "dental_checkup",
                "service_name": "Dental check-up", "service_name_ar": "فحص أسنان", "provider_id": "dr_khoury", "provider_name": "Dr. Rania Khoury",
                "date": "2026-11-05", "start": "16:30", "start_at": "2026-11-05T16:30:00+02:00",
                "patient": {"name": "Lina Haddad", "phone": "0599000111"}}

    async def call_read(self, name, args):
        if name == "clinic_info":
            return {"ok": True, **self.clinic, "today": "2026-11-02", "now": "2026-11-05T08:00:00+02:00" if self.fixture.get("too_late") else "2026-11-02T08:00:00+02:00",
                    "last_bookable_day": "2026-11-23", "providers": self.providers}
        if name == "list_services":
            return {"ok": True, "services": self.services}
        if name == "get_patient":
            record = self.patients.get(args["phone"])
            return {"ok": True, **record} if record else {"ok": False, "error": {"code": "unknown_patient"}}
        if name == "get_booking":
            record = self.bookings.get(args["booking_id"])
            return {"ok": True, **deepcopy(record)} if record else {"ok": False, "error": {"code": "unknown_booking"}}
        if name == "find_slots":
            if self.fixture.get("find_failure"):
                return {"ok": False, "error": {"code": "service_unavailable"}}
            day = args["date_from"]
            if date.fromisoformat(day).weekday() == 6:
                return {"ok": True, "slots": []}
            weekday = date.fromisoformat(day).strftime("%A").lower()
            eligible = [p for p in self.providers if args["service_id"] in p["service_ids"] and weekday in p["working_days"]]
            if not eligible:
                return {"ok": True, "slots": []}
            first, last = eligible[0]["id"], eligible[-1]["id"]
            slots = [{"slot_id": f"SL-EVAL-{day}-{clock}-{provider}", "date": day, "start": clock,
                      "service_id": args["service_id"], "provider_id": provider,
                      "provider_name": next(p["name"] for p in self.providers if p["id"] == provider)}
                     for clock, provider in (("09:00", first), ("15:30", first), ("16:30", first), ("18:00", last))]
            hours = self.clinic["opening_hours"][weekday]
            duration = next(service["duration_min"] for service in self.services if service["id"] == args["service_id"])
            minutes = lambda value: int(value[:2]) * 60 + int(value[3:])
            slots = [slot for slot in slots if minutes(slot["start"]) >= minutes(hours["open"]) and minutes(slot["start"]) + duration <= minutes(hours["close"])]
            return {"ok": True, "slots": [s for s in slots if (not args.get("after") or s["start"] >= args["after"])
                    and (not args.get("before") or s["start"] < args["before"])
                    and (not args.get("provider_id") or s["provider_id"] == args["provider_id"])][:args.get("limit", 5)]}
        raise AssertionError(f"Unexpected read: {name}")

    async def call_write(self, name, args):
        self.writes.append((name, deepcopy(args)))
        if name == "save_patient":
            self.patients[args["phone"]] = deepcopy(args)
            return {"ok": True, **args}
        if name == "cancel_booking":
            record = self.bookings[args["booking_id"]]
            record["status"] = "cancelled"
            return {"ok": True, **record}
        assert name == "create_booking"
        if self.fixture.get("booking_failure"):
            return {"ok": False, "error": {"code": "service_unavailable"}}
        if self.fixture.get("slot_taken"):
            return {"ok": False, "error": {"code": "slot_taken"}}
        key = args["idempotency_key"]
        if key in self.idempotency:
            return self.idempotency[key]
        if args["service_id"] == "pediatrics_visit" and not args.get("guardian_name"):
            return {"ok": False, "error": {"code": "policy_violation"}}
        bid = f"BK-EVAL-{len(self.bookings) + 1}"
        record = {"booking_id": bid, "status": "confirmed",
                  "patient": {"name": args["patient_name"], "phone": args["patient_phone"]},
                  "service_id": args["service_id"], "slot_id": args["slot_id"], "guardian_name": args.get("guardian_name")}
        self.bookings[bid] = record
        self.idempotency[key] = {"ok": True, **record}
        return self.idempotency[key]


# يحول طلبات harness إلى FastAPI الحقيقي وقراءات سجل وهمي؛ أي host غير معروف ممنوع.
def evaluate(cases_file, verbose=False):
    cases = [json.loads(line) for line in cases_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = []
    started = time.time()
    for case in cases:
        gateway = EvalGateway(case.get("fixture"))
        extraction = {turn.get("say", turn.get("transcript")): turn.get("mock_intake", {}) for turn in case["turns"] if "say" in turn or "voice" in turn}
        async def fake_extract(message):
            return IntakeResult.model_validate({"language": "en", "intent": "booking", **extraction.get(message, {})})
        voice_text = next((turn["transcript"] for turn in case["turns"] if "voice" in turn), "hello")
        app.dependency_overrides[get_gateway] = lambda: gateway
        before = set(session_store._sessions)
        try:
            with TestClient(app) as api, patch("agent.graph.extract_request", new=fake_extract), patch("integrations.voice_adapter.transcribe", return_value=voice_text):
                def route(request):
                    if request.url.host == "app.test":
                        response = api.request(request.method, request.url.path, content=request.content,
                                               headers={k: v for k, v in request.headers.items() if k.lower() != "host"})
                        return httpx.Response(response.status_code, json=response.json())
                    if request.url.host == "clinic.test" and request.method == "GET":
                        data = list(gateway.patients.values()) if request.url.path == "/admin/patients" else list(gateway.bookings.values())
                        return httpx.Response(200, json=data)
                    raise AssertionError("The safe harness prohibits external or administrative writes")
                with httpx.Client(transport=httpx.MockTransport(route)) as client:
                    result = run_case(case, {"APP": "http://app.test", "CLINIC_API": "http://clinic.test"}, client, cases_file, None, verbose)
                    results.append(result)
                    print(case["id"], "PASS" if result["passed"] else "FAIL", result.get("error") or "", flush=True)
        finally:
            app.dependency_overrides.pop(get_gateway, None)
            for sid in set(session_store._sessions) - before:
                session_store.delete(sid)
                for store in (app.state.session_locks, app.state.voice_reviews, app.state.voice_generations, app.state.last_audio):
                    store.pop(sid, None)
    return results, started


if __name__ == "__main__":
    cases_file = ROOT / "evals/cases.mandatory.jsonl"
    results, started = evaluate(cases_file, "-v" in sys.argv)
    markdown, summary = write_scorecard(results, SimpleNamespace(cases=cases_file), started)
    (ROOT / "evals/scorecard.safe.md").write_text(markdown, encoding="utf-8")
    (ROOT / "evals/scorecard.safe.json").write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    raise SystemExit(0 if all(result["passed"] for result in results) else 1)
