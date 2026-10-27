# Shared Implementation Plan — Voice Receptionist

**Timeline:** Tuesday, October 27 – Saturday, October 31, 2026  
**Team:** Developer A and Developer B, with comparable experience  
**Project:** Zaytoun Family Clinic Voice Receptionist — Agentic AI Bootcamp

## Original References Reviewed

- Scenario 1 project: `scenario-1-voice-receptionist/README.md` in the supplied materials.
- Clinic, MCP, voice, UI, and evaluation code under `kit/` in the supplied ZIP.
- [Official API contract (CONTRACT.md)](https://github.com/abuzaid268/Agentic-AI-Bootcamp-Students/blob/main/Final-Mini-Project/common/CONTRACT.md).
- [General submission and evaluation requirements](https://github.com/abuzaid268/Agentic-AI-Bootcamp-Students/blob/main/Final-Mini-Project/README.md).
- [Design document template](https://github.com/abuzaid268/Agentic-AI-Bootcamp-Students/blob/main/Final-Mini-Project/common/DESIGN-DOC-TEMPLATE.md).
- [Final README template](https://github.com/abuzaid268/Agentic-AI-Bootcamp-Students/blob/main/Final-Mini-Project/common/PORTFOLIO-README-TEMPLATE.md).
- [Demo and video guide](https://github.com/abuzaid268/Agentic-AI-Bootcamp-Students/blob/main/Final-Mini-Project/common/DEMO-GUIDE.md).

## 1. Non-Negotiable Requirements

1. **At least two specialized agent roles:** an Intake role to gather request information and a Scheduler role to check availability, using an LLM in a multi-step process with explicit state.
2. **MCP:** connect to the clinic through MCP, not through direct REST calls from agent logic.
3. **Real voice interaction:** record/upload audio, speech-to-text (STT), conversation, and text-to-speech (TTS). At least one English scenario must work; basic Arabic booking support is required, while a fully polished Arabic voice experience is an enhancement.
4. **Memory:** preserve conversational context across turns and returning-patient information through `get_patient`/`save_patient` or clinic records.
5. **Approval gate:** the LLM must never call a write tool directly; only `/api/approve` may invoke `create_booking` or `cancel_booking` using a stored, immutable `action_id` and a matching idempotency key.
6. **Safety guardrails:** no diagnosis or medication advice; use the prescribed emergency response; resist prompt injection; never invent an ID.
7. **At least 10 tests:** pass the supplied scenarios as far as possible; add your own tests, a scorecard, and a before/after description of one fixed failure.
8. **Delivery:** FastAPI endpoints `/health`, `/api/chat`, `/api/approve`, `/api/voice`; `docker compose up --build` startup; working Gradio UI; README; DESIGN.md; architecture diagram; public GitHub repository; a three-minute video; and a presentation involving both developers.
9. **Original kit:** do not modify any files under `services/`. Do not call `/admin/*` from the application; reserve it for tests only.

## 2. Balanced Workload: 22 Effort Points Per Developer

Effort points are estimates, not guaranteed hours. Reassess bottlenecks after the first integration, and redistribute a small task if necessary.

### Developer A — 22 Points

| ID | Task and owned files | Points | Completion evidence |
|---|---|---:|---|
| A1 | Intake role and structured extraction (`agent/intake.py`) | 5 | Extract service, date, time, name, and phone from English or Arabic input; ask one clarification question when information is missing. |
| A2 | LangGraph orchestration, state, dates, and sessions (`agent/graph.py`, `agent/state.py`, `agent/dates.py`, `core/session_store.py`) | 5 | Conversation continues across turns; interpret dates using `clinic_info.today` rather than the machine's real clock. |
| A3 | Approval service (`actions/approval.py`) | 5 | Store pending actions, reject with no write, approve through MCP, safely reuse the same `action_id`, and prevent double-booking from repeated approval. |
| A4 | Guardrails and returning-patient memory (`agent/guardrails.py`, integrated into graph) | 4 | No diagnosis/medication advice, emergency instruction 101, resist injection, use patient name/doctor preference, and request a guardian for a child. |
| A5 | Unit tests and DESIGN.md (`tests/unit/test_intake.py`, `tests/unit/test_approval.py`, `DESIGN.md`) | 3 | Independent unit tests and a shared, reviewable design document. |
| **Total** | | **22** | |

### Developer B — 22 Points

| ID | Task and owned files | Points | Completion evidence |
|---|---|---:|---|
| B1 | MCP gateway and tracing (`integrations/mcp_gateway.py`) | 5 | Open a stdio session, return `structuredContent`, separate read and write operations, and log tool name, success, and latency. |
| B2 | Scheduler role (`agent/scheduler.py`) | 5 | Call `find_slots`, validate actual `slot_id` values, respect clinic/provider hours, and propose legitimate alternatives without returning a pending action before a choice is made. |
| B3 | FastAPI, voice, and UI integration (`app/main.py`, `integrations/voice_adapter.py`) | 5 | Endpoints match the contract; `/api/voice` transcribes speech, calls the **same** function used by `/api/chat`, and speaks the response. |
| B4 | Docker and startup (`Dockerfile`, `docker-compose.yml`, `.env.example`, `requirements.txt`) | 4 | A single command starts clinic, backend, and UI; a fresh clone works. |
| B5 | Integration tests, evaluations, and README (`tests/integration/*`, `README.md`) | 3 | Ten scenario runs, scorecard, an account of one corrected failure, setup instructions, and demo instructions. |
| **Total** | | **22** | |

**Mandatory shared work:** each developer reviews the other's PR, runs part of the other's implementation, and explains at least one aspect of it during the presentation. If either track falls behind on Friday, transfer a small self-contained task (test, documentation, or endpoint). Never transfer ownership of a file or edit somebody else's files without coordination.

## 3. File Ownership — Avoid Git Conflicts

```text
kit/
├── app/
│   └── main.py                     B
├── agent/
│   ├── intake.py                   A  (Intake Agent)
│   ├── scheduler.py                B  (Scheduler Agent)
│   ├── graph.py                    A
│   ├── state.py                    A
│   ├── dates.py                    A
│   └── guardrails.py               A
├── actions/
│   └── approval.py                 A
├── core/
│   ├── contracts.py                A creates; BOTH approve changes
│   └── session_store.py            A
├── integrations/
│   ├── mcp_gateway.py              B
│   └── voice_adapter.py            B
├── tests/
│   ├── unit/                       A
│   └── integration/                B
├── DESIGN.md                       A; B reviews
├── README.md                       B; A reviews
├── Dockerfile                      B
├── docker-compose.yml              B
├── .env.example                    B
├── requirements.txt                B
├── services/                       ORIGINAL — no edits
├── voice/                          ORIGINAL — no edits unless agreed
├── ui/                             ORIGINAL — no edits unless agreed
└── evals/                          ORIGINAL runner/cases; add own tests separately
```

**Rule:** two developers must not edit the same file at the same time. Shared-change files (`core/contracts.py`, `DESIGN.md`, `docker-compose.yml`) require a quick agreement: one owner makes the edit, and the other reviews it. Never commit `.env`, `.venv`, or downloaded Piper models.

## 4. Freeze the Integration Contracts Before Parallel Work

**Internal A–B interfaces — initial proposal to finalize by the end of day one:**

```python
# A provides:
async def handle_message(session_id: str, message: str, gateway) -> dict: ...
async def handle_approval(session_id: str, action_id: str, approved: bool, gateway) -> dict: ...

# B provides:
class ClinicGateway:
    async def call_read(self, tool_name: str, args: dict) -> dict: ...
    async def call_write(self, tool_name: str, args: dict) -> dict: ...

async def schedule(state: dict, gateway) -> dict: ...
```

These are **internal application interfaces**, not the instructor's external API contract. Developer B must preserve the external HTTP contract **exactly**; Developer A must return compatible JSON from the functions above.

**Unchanged external HTTP contract:**

- `GET /health` → `{ "status": "ok" }`.
- `POST /api/chat` → JSON `{session_id,message,user?}`.
- `POST /api/approve` → `{session_id,action_id,approved}`.
- `POST /api/voice` → multipart `{session_id,audio}`.
- All conversation responses → `session_id`, `reply`, `state?`, `pending_action?`, `result?`, `tool_calls`, `usage?`.
- `pending_action` exactly `{action_id,type,summary,payload}`. Booking type: `create_booking`. Its frozen payload includes `slot_id`, `service_id`, `patient_name`, `patient_phone`, and `guardian_name` when required.
- `/api/voice` additionally returns `transcript`, `audio_b64`, and `audio_mime`.

**Enforcement rules:** `/api/approve` must not trust a booking payload submitted by the browser. It must retrieve the pending action from `SessionStore` using `session_id` and `action_id`. On success, `result.booking_id` comes only from the MCP response. On rejection, 409, or 503, there is no fabricated success ID. Repeating the same approval must not repeat the write. `/api/voice` must reuse `handle_message` instead of duplicating booking logic.

### How Each Developer Can Work Without Waiting

- Agree on `ChatResponse` and `Gateway` shapes on day one.
- **A** initially uses `FakeGateway` to return representative fake `clinic_info`/`get_patient`/`find_slots` results resembling actual outputs.
- **B** initially uses `FakeAgent` to return known `reply` and `pending_action` values and test `/api/chat`, `/api/approve`, and `/api/voice`.
- Replace these stubs after the first integration; do not ship fake implementations in production mode.

## 5. Daily Schedule with Integration Gates

| Date | Developer A | Developer B | Exit criteria |
|---|---|---|---|
| **Tue 27 Oct** | Draft DESIGN.md and `core/contracts.py`; define agent states and clarification response format. | GitHub repo/branches, Python 3.12 setup, run clinic on `:8101`, run `check_kit.py`, MCP smoke test, begin FastAPI health endpoint. | Frozen contract, working repo on both machines, both can start the clinic locally, Check-in 0 if scheduled. |
| **Wed 28 Oct — morning** | Implement `intake.py` and `state.py`, structured extraction, and FakeGateway. | Implement `mcp_gateway.py` and `scheduler.py` with FakeAgent. | Each unit can be tested independently without waiting for the other. |
| **Wed 28 Oct — evening** | Make `graph.py` call Scheduler and preserve conversation state. | Connect `/api/chat` to the graph; run a text request through the UI. | **Walking skeleton:** UI → API → two roles → MCP `find_slots` → grounded response; record a 30-second clip (Check-in 1 if scheduled). |
| **Thu 29 Oct — morning** | Implement `approval.py` and SessionStore; reject/approve/double-click handling, phone and preferences. | Wire STT/TTS and `/api/voice`; test supplied WAV files; prepare Docker services. | Complete text booking with a real `BK-` ID, and rejection with no booking. |
| **Thu 29 Oct — evening** | Guardrails for emergencies, injection, and children; cooperate on 409/503 handling. | Run initial S1-01…S1-10 evaluations and integration checks. | First scorecard, even if not all pass; identify the top three failures. |
| **Fri 30 Oct — morning** | Fix date handling, multiple-turn behavior, and safety; add unit tests. | Docker + Gradio through Compose; integration tests; fix STT failures. | Core cases stable as far as possible; any failures recorded and categorized. |
| **Fri 30 Oct — evening** | Update DESIGN, explain role boundaries, review output/readback. | Final README, startup diagram, scorecard. | Clean-clone run, one documented before/after bug fix, three-minute video, and presentation roles assigned. |
| **Sat 31 Oct** | Review B's work; test context restoration and safety. | Review A's work; test approval, voice, and Docker. | Stop adding features; final run, `v1.0` tag, submission, and presentation. |

**When time is tight:** use `TTS_PROVIDER=browser` instead of local Piper, do not build a new frontend, and defer rescheduling and Google Calendar. Do **not** drop audio input, the booking approval gate, two agent roles, Docker, or evaluations; those are core requirements.

## 6. Test Ownership

- **A starts with:** `S1-02` clarification, `S1-04` rejection, `S1-06` emergency, `S1-07` memory, `S1-10` injection. Add at least two unit tests covering multi-turn messages and double approval.
- **B starts with:** `S1-01` real booking, `S1-03` conflict/alternatives, `S1-05` 503, `S1-08` audio, `S1-09` Friday hours. Add at least two integration tests for 409 and `tool_calls` output.
- After integration, **both developers run all ten**. A failing case isn't automatically assigned to the person owning its test; the fix belongs to the person who owns the failing source file.
- Potential hidden tests: repeated approval, `slot_taken` after the offer, under-16/guardian, cancellation within 24 hours, injected instructions, Arabic requests. Do not inspect only the AI's response text: use `/admin/bookings` **in tests only** to verify that no unauthorized booking happened.

## 7. Git Strategy and Continuous Integration

**One shared GitHub repository**; keep `main` working at all times. Use a short-lived feature branch for each small task, with a pull request reviewed by the other developer. Do not keep two long-lived branches separate until Friday.

### Initial Repository Setup

One developer creates a public repo and invites the other as a collaborator. The plan proposes copying **only the contents of `kit/`** into the repository root; the initial shared commit should contain the unmodified kit. Then each developer works from an independent clone.

```bash
git clone <REPO_URL>
cd <REPO_NAME>
git switch -c feat/intake
# edit only the files you own
git add agent/intake.py tests/unit/test_intake.py
git commit -m "feat: extract patient booking details"
git push -u origin feat/intake
```

On GitHub: create a Pull Request → request review from the other developer → merge. After the merge:

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/next-small-task
```

To update a still-active branch without overwriting a teammate's changes:

```bash
git fetch origin
git merge origin/main
# resolve any conflict, run tests, commit if necessary
```

**Merge rules:** (1) don't edit original files in `services/`; (2) never commit `.env`; (3) keep PRs to two or three files when feasible; (4) run `python check_kit.py` and `python evals/run_evals.py --only ... -v` when services are available; (5) don't merge if `/health` is broken or a previously working flow stops working; (6) resolve a file conflict with its owner and have the other developer review.

**Integration cadence:** at least twice daily, once mid-session and once at the end. Hold a 10-minute review for each integration, with three questions: (a) What did you finish? (b) What is blocking your teammate? (c) Which file will you edit next?

**Do not share a single Clinic API during tests.** Each developer runs the API on their own machine with `CLINIC_TODAY=2026-11-02`, because `admin/reset` and test calls change in-memory booking state and can invalidate the other person's tests.

## 8. Basic Run Instructions (Windows PowerShell)

From the repository root after putting the `kit/` contents there:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`, set `OPENROUTER_API_KEY`, then:

```env
CLINIC_TODAY=2026-11-02
TTS_PROVIDER=browser
```

Use three terminals with the virtual environment active:

```powershell
# Terminal 1
uvicorn services.clinic_api:app --port 8101
# Terminal 2
python check_kit.py
uvicorn app.main:app --port 8000
# Terminal 3
python ui/app_ui.py
```

Once the first integrated flow works:

```powershell
python evals/run_evals.py --only S1-01 -v
python evals/run_evals.py
python evals/run_evals.py --repeat 3
```

`check_kit.py` verifies API/MCP connectivity and voice configuration. It **does not** prove that real audio transcription works; an actual WAV test is required on Thursday.

## 9. Definition of Ready / Definition of Done

### Ready to Start

- The task is defined; the file owner is known; inputs/outputs are agreed; and there is a test example.
- If a teammate's component is not available yet, there is a temporary mock.

### Ready to Merge (Done)

- Code works locally and respects contracts.
- A small test or reproducible verification was added.
- No secrets in the commit; no edits to the original kit.
- The other developer reviewed and approved the PR.
- Smoke tests pass after merge; no booking occurs without approval.

### Ready to Submit

- All six official Definition of Done items are present.
- One Docker command starts the clinic, API, and UI.
- Scorecard includes 10+ cases and a before/after failure example.
- README, DESIGN, architecture diagram, video, and `real vs simulated` table are included.
- Both developers can explain agent roles, MCP, and why a booking requires approval.
- Check-in 0/1/2 follow the coach's actual schedule; do not assume these dates are the check-in dates.

## 10. Top Risks and Mitigations

| Risk | Early mitigation |
|---|---|
| A and B disagree on JSON shape | Freeze `core/contracts.py` and document the API before implementation. |
| A waits for Scheduler or B waits for Agent | FakeGateway and FakeAgent; test independently. |
| Date interpretation differs | Use `CLINIC_TODAY=2026-11-02` in each environment, and derive dates from `clinic_info`. |
| Double booking from a repeated click | Stored `action_id`, session lock, Idempotency-Key = `action_id`, no second write after success. |
| Model text claims a booking happened when it did not | Form the final confirmation solely from the MCP response; add output guardrail. |
| API returns 409 or 503 | Explain the failure; never return a fake `booking_id`; allow recovery or a new search. |
| STT or Piper fails | Test an audio sample early, prefer browser TTS for the demo, don't leave it until Friday. |
| Docker fails at the end | Add app and UI to Compose by Thursday, not hours before submission. |
| Git conflicts | File ownership, small PRs, merge twice daily, no direct work on `main`. |
| Costs or secret leaks | Set a low OpenRouter spending limit; keep `.env` local only. |

**Team rule:** end every day with a version that works better than the morning version. No new feature is allowed to break the core path: text → real slot → `pending_action` → Approve → real `BK-` ID.
