# API reference

Base URL `http://localhost:8000`. Interactive OpenAPI docs are at `/docs`. All request and response bodies are JSON,
except the SSE stream.

```bash
BASE=http://localhost:8000
```

**Errors** use FastAPI's shape, `{"detail": "..."}`:

| Code | When | Example `detail` |
|---|---|---|
| 400 | Bad input the schema cannot catch | `unknown scenario 'nope'; choose one of [...]` |
| 404 | Unknown run id | `run zzz not found` |
| 409 | Action not allowed in the run's current status | `run is 'running', no approval pending` / `cannot go from 'completed' to 'paused'` |
| 422 | Body fails validation (e.g. `decision` not approve/reject, empty guidance) | Pydantic error list |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and LLM mode |
| GET | `/api/scenarios` | The four simulated incidents |
| GET | `/api/tools` | Tool schemas and risky flag |
| GET | `/api/memory` | Long-term memory of past incidents |
| POST | `/api/runs` | Start a run |
| GET | `/api/runs` | List runs, newest first |
| GET | `/api/runs/{id}` | Full run view |
| GET | `/api/runs/{id}/events?after=N` | Saved trace events with id > N |
| GET | `/api/runs/{id}/stream?after=N` | Server-Sent Events: live text deltas and trace events |
| POST | `/api/runs/{id}/pause` | Pause at the next step boundary |
| POST | `/api/runs/{id}/resume` | Resume a paused or failed run |
| POST | `/api/runs/{id}/cancel` | Cancel a run that is not finished |
| POST | `/api/runs/{id}/approval` | Approve or reject the pending risky action |
| POST | `/api/runs/{id}/guidance` | Send the agent a message |

## Meta

### `GET /health`
```bash
curl -s $BASE/health
```
```json
{"status": "ok", "llm_mode": "mock", "model": "mock-policy-v1"}
```
`llm_mode` is `gemini` when `GEMINI_API_KEY` is set; `model` is then the Gemini model name.

### `GET /api/scenarios`
```json
[{"id": "bad-deploy", "title": "Checkout failing: payments-api 5xx spike",
  "alert": "PagerDuty: payments-api error rate > 5% for 10 minutes. Customers report checkout failures."}, "..."]
```
Ids: `bad-deploy`, `memory-leak`, `traffic-spike`, `ghost-alert`.

### `GET /api/tools`
The ten tools exactly as the model sees them, plus the `risky` flag the approval gate enforces.
```json
[{"name": "rollback_deployment", "description": "...",
  "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "to_version": {"type": "string"}},
                 "required": ["service", "to_version"]},
  "risky": true}, "..."]
```

### `GET /api/memory`
One row per service from each run that ended `completed`.
```json
[{"id": 1, "service": "payments-api", "symptom": "PagerDuty: payments-api error rate > 5% ...",
  "root_cause": "Deploy v2.4.0 of payments-api ... introduced errors ...",
  "resolution": "payments-api rolled back to v2.3.9", "run_id": "96e9f2a9e5", "created_at": 1794914392.68}]
```

## Runs

### `POST /api/runs`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `scenario` | string | yes | One of the scenario ids |
| `alert` | string | no | Your own alert text; replaces the scenario's alert in the prompt |

```bash
curl -s -X POST $BASE/api/runs -H 'Content-Type: application/json' -d '{"scenario":"bad-deploy"}'
```
`201` with the run view (below), `status: "running"`, `step: 0`. Unknown scenario: `400`.

### `GET /api/runs`
```json
[{"id": "404d6a066f", "scenario": "bad-deploy", "title": "Checkout failing: payments-api 5xx spike",
  "status": "completed", "step": 7, "tokens_in": 7805, "tokens_out": 548, "cost_usd": 0.003711,
  "llm_mode": "mock", "created_at": 1794914322.27, "updated_at": 1794914322.60}]
```

### `GET /api/runs/{id}` (run view)
Abridged from a completed `bad-deploy` run:
```json
{
  "id": "404d6a066f", "scenario": "bad-deploy", "title": "Checkout failing: payments-api 5xx spike",
  "alert": "PagerDuty: ...", "status": "completed", "llm_mode": "mock", "error": null,
  "step": 7, "tokens_in": 7805, "tokens_out": 548, "cost_usd": 0.003711,
  "plan": ["List services and find which ones are unhealthy", "..."],
  "steps": [{"n": 3, "thought": "payments-api is unhealthy. Gathering ... in parallel.",
             "calls": [{"tool": "get_metrics", "args": {"service": "payments-api"}, "risky": false, "ok": true,
                        "summary": "payments-api: error 18.5%, p99 1900ms, ...", "data": {"...": "..."},
                        "attempts": 2, "latency_ms": 301}]}],
  "notes": [],
  "pending_approval": null,
  "approvals": [{"id": "86e5ac1b32", "step": 5, "tool": "rollback_deployment",
                 "args": {"service": "payments-api", "to_version": "v2.3.9"}, "reason": "Runbook agrees. ...",
                 "status": "executed", "comment": "ok", "created_at": 1794914322.4, "decided_at": 1794914322.5}],
  "report": {"root_cause": "Deploy v2.4.0 of payments-api ...", "severity": "SEV2", "confidence": 0.9,
             "affected_services": ["payments-api"], "actions_taken": ["payments-api rolled back to v2.3.9"],
             "follow_ups": ["Fix the bug in v2.4.0 and add a test", "..."], "routed_to_human": false,
             "routing_reason": "mitigated and verified with high confidence"},
  "answer": {"diagnosis": "Deploy v2.4.0 of payments-api ...", "action_taken": "payments-api rolled back to v2.3.9",
             "outcome": "Fixed and verified: mitigated and verified with high confidence", "confidence": 0.9},
  "sources": [{"step": 3, "source": "Metrics", "text": "payments-api: error 18.5%, p99 1900ms, ..."}, "..."],
  "done": [{"step": 5, "text": "payments-api rolled back to v2.3.9"}],
  "not_done": [],
  "infra": {"payments-api": {"status": "healthy", "version": "v2.3.9", "replicas": 3}, "...": {}}
}
```

| Field | Meaning |
|---|---|
| `status` | `pending`, `running`, `waiting_approval`, `paused`, `failed`, `completed`, `escalated`, `cancelled` |
| `plan` | Current plan steps |
| `steps` | Checkpointed step records: thought and tool calls with results |
| `notes` | Runtime notes fed back to the model (rejections, errors, loop guard) |
| `pending_approval` | The approval waiting for a decision, or `null` |
| `approvals` | Every approval for the run: `pending`, `approved`, `rejected`, `executed`, `cancelled` |
| `report` | The `finish_triage` report plus routing, or `null` while running |
| `answer` | Final answer derived from the report, or `null` while running |
| `sources` | Successful read calls that back the findings, with step number |
| `done` / `not_done` | Risky actions that ran / failed, and closing notes (no change proposed, handed to a human) |
| `infra` | Current simulated service state |

### `GET /api/runs/{id}/events?after=N`
Saved trace events, oldest first. Use the last `id` as `after` to page.
```json
[{"id": 4, "run_id": "96e9f2a9e5", "ts": 1794914392.36, "step": 1, "type": "plan",
  "data": {"steps": ["List services and find which ones are unhealthy", "..."]}}]
```

### `GET /api/runs/{id}/stream?after=N` (Server-Sent Events)
```bash
curl -N $BASE/api/runs/$RUN/stream
```
```
event: token
data: {"seq": 12, "step": 3, "kind": "thought", "text": "is unhealthy. Gathering "}

id: 9
event: trace
data: {"id": 9, "run_id": "96e9f2a9e5", "ts": 1794914392.36, "step": 3, "type": "thought", "data": {"text": "..."}}

event: end
data: {}
```

| SSE event | Payload | Notes |
|---|---|---|
| `token` | `{seq, step, kind, text}`, `kind` = `plan`, `thought` or `answer` | Live text delta. In memory only; concatenating the deltas of one `step` and `kind` gives the text of the matching saved event |
| `trace` | A saved event (same shape as `/events`) | `id:` is set, so a reconnecting client can pass `after` |
| `end` | `{}` | Sent about one second after the run stops at `completed`, `escalated`, `failed` or `cancelled` |

Comment lines (`: keep-alive`) are sent while nothing changes. Deltas for a step are always sent before that step's
saved event.

**Trace event types** (`type` field):

| Type | `data` |
|---|---|
| `run_created` | `scenario`, `alert`, `llm` |
| `llm_call` | `model`, `tokens_in`, `tokens_out`, `latency_ms`, `cost_usd`, `context_layers`, `memories_used` |
| `thought` | `text` |
| `plan`, `plan_revised` | `steps` |
| `tool_call` | `tool`, `args`, `risky` |
| `tool_result` | `tool`, `args`, `ok`, `summary`, `error`, `attempts`, `latency_ms`, `risky`, `output` |
| `retry` | `tool`, `attempts`, `ok` |
| `warning` | `message` (token budget, unknown tool, loop guard, possible prompt injection) |
| `approval_requested` | `tool`, `args`, `reason` |
| `approval_decided` | `tool`, `args`, `decision`, `comment` |
| `guidance` | `message` |
| `status` | `from`, `to`, `why` |
| `error` | `message` |
| `run_finished` | `report` |
| `memory_saved` | `services` |
| `answer` | `diagnosis`, `action_taken`, `outcome`, `confidence`; always the last event of a finished run |
| `resumed_after_restart` | `message` |

## Human in the loop

### `POST /api/runs/{id}/approval`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `decision` | `approve` or `reject` | yes | |
| `comment` | string | no | Shown to the agent at its next step |

```bash
curl -s -X POST $BASE/api/runs/$RUN/approval -H 'Content-Type: application/json' \
     -d '{"decision":"approve","comment":"ok, roll back"}'
```
`200` with the approval row:
```json
{"id": "abeb4e1736", "run_id": "96e9f2a9e5", "step": 5, "tool": "rollback_deployment",
 "args": {"service": "payments-api", "to_version": "v2.3.9"}, "reason": "Runbook agrees. ...",
 "status": "approved", "comment": "ok, roll back", "created_at": 1794914392.66, "decided_at": 1794914392.67}
```
`409` if the run is not `waiting_approval`. The action runs once, when the worker resumes, and the row then becomes
`executed`.

### `POST /api/runs/{id}/guidance`
```bash
curl -s -X POST $BASE/api/runs/$RUN/guidance -H 'Content-Type: application/json' \
     -d '{"message":"Prefer rollback over restart"}'
```
`200` `{"ok": true}`. `message` is 1-500 characters. The agent reads it as trusted operator input at its next step.
`409` if the run has finished.

## Lifecycle

All three return the run view. `409` if the transition is not allowed from the current status.

| Call | From | To |
|---|---|---|
| `POST /api/runs/{id}/pause` | `running` | `paused` (stops at the next step boundary) |
| `POST /api/runs/{id}/resume` | `paused`, `failed` | `running` (continues from the last checkpoint) |
| `POST /api/runs/{id}/cancel` | any status that is not final | `cancelled` (a pending approval is cancelled too) |

## Restart check from a terminal

```bash
# 1. start a run and wait until it is running or waiting_approval
# 2. stop the server (Ctrl+C) and start it again: make run
curl -s $BASE/api/runs/$RUN | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["step"])'
# 3. a run that was mid-step continues on its own and its trace shows resumed_after_restart;
#    a run that was waiting_approval keeps waiting until you approve or reject
```
