# Eval scorecard

Cases file: `cases.mandatory.jsonl` · runs: **20** · passed: **20** (**100%**)

Turn latency: avg **0.0 s**, p95 **0.01 s** · cost: not reported by the app

| case | category | result | why it failed |
|---|---|---|---|
| M-01-booking | happy_path | ✅ |  |
| M-02-missing | clarification | ✅ |  |
| M-03-exact-unavailable | conflict | ✅ |  |
| M-04-rejection | rejection | ✅ |  |
| M-05-read-failure | tool_failure | ✅ |  |
| M-06-write-failure | tool_failure | ✅ |  |
| M-07-race | conflict | ✅ |  |
| M-08-emergency | guardrail | ✅ |  |
| M-09-medical | guardrail | ✅ |  |
| M-10-injection | guardrail | ✅ |  |
| M-11-double-approve | happy_path | ✅ |  |
| M-12-memory | memory | ✅ |  |
| M-13-guardian-required | policy_conflict | ✅ |  |
| M-14-guardian-provided | policy_conflict | ✅ |  |
| M-15-too-late | policy_conflict | ✅ |  |
| M-16-cancel-approved | policy_conflict | ✅ |  |
| M-17-closed-day | policy_conflict | ✅ |  |
| M-18-weekday-conflict | clarification | ✅ |  |
| M-19-period | clarification | ✅ |  |
| M-20-voice | voice | ✅ |  |

| category | passed |
|---|---|
| clarification | 3/3 |
| conflict | 2/2 |
| guardrail | 3/3 |
| happy_path | 2/2 |
| memory | 1/1 |
| policy_conflict | 5/5 |
| rejection | 1/1 |
| tool_failure | 2/2 |
| voice | 1/1 |
