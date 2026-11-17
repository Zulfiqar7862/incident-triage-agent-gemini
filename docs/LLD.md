# Low-level design: Incident Triage Agent

## 1. Module map

```mermaid
classDiagram
    class Runtime {
      +start_run(scenario, alert)
      +pause(run_id)
      +resume(run_id)
      +cancel(run_id)
      +decide(run_id, approve, comment)
      +add_guidance(run_id, msg)
      +recover() list
      -_spawn(run_id)
      -_drive(run_id)
    }
    class Agent {
      +llm; +registry
      +step(run_id) "continue|stop"
      -_resume_gated_action(rs)
      -_record_decision(rs, decision)
      -_classify(rs, call) read|risky|done
      -_run_reads(rs, calls)
      -_request_approval(rs, risky_calls)
      -_finish(rs, report_args)
      -_stream(run_id, step, kind, text)
    }
    class RunState {
      run; state; plan; step; tokens_in; tokens_out
      +load(run)$
    }
    class Checkpointer {
      +save(rs, executed_approval)
    }
    class Planner {
      +services_in_scope(rs)
      +next_decision(rs, guidance) decision, layers, memories
    }
    class Executor {
      +run(world, call) record
      +run_parallel(world, calls) records
    }
    class ApprovalGate {
      +request(rs, call, reason)
      +decision(rs) status, approval
      +rejected_record(rs, status, approval)
      +close(rs, record)
    }
    class guards {
      <<module>>
      +limit_reached(rs, settings)
      +near_token_budget(rs, settings)
      +is_repeated(steps, call)
    }
    class LLM {
      <<interface>>
      +decide(system, prompt, tools, state, on_text) LLMDecision
    }
    class TokenStream {
      +publish(run_id, step, kind, text)
      +since(run_id, seq) list
    }
    class GeminiLLM
    class MockLLM
    class ToolRegistry {
      +register(Tool)
      +get(name) Tool
      +schemas() list
    }
    class Tool {
      name; description; parameters; risky; fn
    }
    class DB {
      +create_run() +get_run() +save_checkpoint()
      +set_status(new, expected)
      +add_event() +events(after)
      +create_approval() +decide_approval()
      +add_memory() +search_memory()
    }
    Runtime --> Agent
    Runtime --> DB
    Agent --> Planner
    Agent --> Executor
    Agent --> ApprovalGate
    Agent --> Checkpointer
    Agent --> TokenStream
    Agent ..> guards
    Agent ..> RunState
    Planner --> LLM
    Executor --> ToolRegistry
    ApprovalGate --> DB
    Checkpointer --> DB
    LLM <|.. GeminiLLM
    LLM <|.. MockLLM
    ToolRegistry o-- Tool
```

| File | Main contents |
|---|---|
| `src/incident_triage_agent/config.py` | `Settings` dataclass (env + `.env` via python-dotenv). `llm_mode` = gemini if key else mock |
| `src/incident_triage_agent/db.py` | `DB` class, schema, conditional status updates, transactions |
| `src/incident_triage_agent/agent/runtime.py` | `Runtime` - lifecycle (start, pause, resume, cancel, decide, guidance), worker threads, `recover()` |
| `src/incident_triage_agent/agent/loop.py` | `Agent.step()` - the thin step loop that calls the modules below; `_finish()` builds the report, routes, saves memory and emits the answer |
| `src/incident_triage_agent/agent/tokens.py` | `TokenStream` - in-memory, per-run buffer of text deltas (max 2,000 per run) |
| `src/incident_triage_agent/agent/answer.py` | `Answer` (Pydantic) and `build_answer(status, report)` |
| `src/incident_triage_agent/agent/sources.py` | `summarize(state, report)` -> `{sources, done, not_done}` |
| `src/incident_triage_agent/agent/state.py` | `RunState` (typed view of a `runs` row), `Checkpointer.save()`, `cost_usd()` |
| `src/incident_triage_agent/agent/planner.py` | `Planner.next_decision()` - memory recall + `build_context()` + `llm.decide()` |
| `src/incident_triage_agent/agent/executor.py` | `Executor.run()` / `run_parallel()` - registry, validation, retry/backoff, injection flag |
| `src/incident_triage_agent/agent/approval.py` | `ApprovalGate` (pending approval, rejected-action record) and `route()` (completed vs escalated) |
| `src/incident_triage_agent/agent/guards.py` | `limit_reached()`, `near_token_budget()`, `is_repeated()` (loop guard) |
| `src/incident_triage_agent/agent/context.py` | `build_context()` -> `(prompt, layer_stats)` |
| `src/incident_triage_agent/agent/prompts.py` | `SYSTEM_PROMPT` |
| `src/incident_triage_agent/agent/safety.py` | `looks_like_injection()`, `redact()` |
| `src/incident_triage_agent/llm/base.py` | `LLM` protocol, `LLMDecision`, `ToolCall`, `chunks()` (split text into word-sized deltas) |
| `src/incident_triage_agent/llm/gemini.py` | `GeminiLLM`, `collect_stream()`, `parse_response()`, `retry_delay()` |
| `src/incident_triage_agent/llm/mock.py` | `MockLLM` rule-based policy; streams its thought in 3-word chunks |
| `src/incident_triage_agent/tools/registry.py` | `Tool`, `ToolRegistry`, `validate_args()`, `execute_with_retry()` |
| `src/incident_triage_agent/tools/infra_tools.py` | tool functions + `build_registry()` |
| `src/incident_triage_agent/sim/world.py` | `SCENARIOS`, `build_world()` |

## 2. Data model (SQLite)

```mermaid
erDiagram
    runs ||--o{ events : "trace"
    runs ||--o{ approvals : "gated actions"
    runs ||--o{ memory : "promoted lessons"
    runs {
      TEXT id PK
      TEXT scenario
      TEXT title
      TEXT alert
      TEXT status
      TEXT llm_mode
      JSON plan
      JSON state "checkpoint"
      INT step
      INT tokens_in
      INT tokens_out
      REAL cost_usd
      JSON report
      TEXT error
      REAL created_at
      REAL updated_at
    }
    events {
      INT id PK "autoincrement = cursor for SSE"
      TEXT run_id
      REAL ts
      INT step
      TEXT type
      JSON data
    }
    approvals {
      TEXT id PK
      TEXT run_id
      INT step
      TEXT tool
      JSON args
      TEXT reason
      TEXT status "pending|approved|rejected|executed|cancelled"
      TEXT comment
      REAL created_at
      REAL decided_at
    }
    memory {
      INT id PK
      TEXT service
      TEXT symptom
      TEXT root_cause
      TEXT resolution
      TEXT run_id
      REAL created_at
    }
```

### The checkpoint (`runs.state` JSON) = short-term memory
```json
{
  "world":   {"services": {...}, "deployments": [...], "flaky": {...}, "actions_applied": [...]},
  "steps":   [{"n": 3, "thought": "payments-api is unhealthy...",
               "calls": [{"tool": "get_metrics", "args": {"service": "payments-api"}, "ok": true,
                          "summary": "payments-api: error 18.5% ...", "data": {...},
                          "attempts": 2, "latency_ms": 301, "risky": false, "flagged": false}]}],
  "notes":   ["The operator rejected restart_service(...): not during peak. Do not retry..."],
  "pending": {"approval_id": "ab12", "tool": "rollback_deployment", "args": {...}, "step": 5}
}
```
`plan`, `step`, token counters and cost are columns, saved in the same `UPDATE`.
Guidance is stored only as `guidance` events (written by the API thread), so the worker never overwrites it.

### Event types
Saved in `events` (payloads in [API.md](API.md#get-apirunsidstreamaftern-server-sent-events)):
`run_created, status, plan, plan_revised, llm_call, thought, tool_call, tool_result, retry, warning,
approval_requested, approval_decided, guidance, error, run_finished, memory_saved, answer, resumed_after_restart`.

Not saved: `token` deltas `{seq, step, kind: plan|thought|answer, text}`, kept only in `TokenStream`.

Order within a run: `run_created`, then for step 1 `llm_call`, `thought`, `plan` (the planner offers only
`update_plan` until a plan exists); finished runs end with `run_finished`, `memory_saved` (if completed) and `answer`.

### Streaming
1. `Planner.next_decision()` passes `on_text` to `llm.decide()`. Gemini calls `generate_content_stream`;
   `collect_stream()` forwards each text part to `on_text` and merges all parts into one response, so
   `parse_response()` and function-call handling are unchanged. Function calls are never streamed partially.
   The mock splits its thought with `chunks()`.
2. `Agent._delta()` publishes each piece to `TokenStream` for step `rs.step + 1`, then sleeps `STREAM_DELAY_SECONDS`.
3. The plan (joined with newlines) and the answer's diagnosis are streamed by `Agent._stream()` just before their
   saved events.
4. The SSE handler sends new deltas before new events on every 100 ms tick, so deltas always precede their event.
   The UI shows deltas with a caret and replaces them with the saved text when the event arrives.
5. Tests assert the deltas of each step and kind join to exactly the saved text.

### Answer and sources
- `build_answer()`: `diagnosis` = report root cause; `action_taken` = applied actions or "No change was made";
  `outcome` = "Fixed and verified: ..." when completed, otherwise "Handed to a human: <routing reason>";
  `confidence` from the report.
- `summarize()`: every successful read of list_services, get_metrics, get_logs, get_recent_deployments or
  search_runbooks is a source (with step). Risky calls that ran go to `done`, failed ones to `not_done`; a pending
  call is skipped. With a report and no risky action, `not_done` says no change was proposed; a run routed to a
  human adds the routing reason.

## 3. Run state machine

```mermaid
stateDiagram-v2
    [*] --> pending: POST /api/runs
    pending --> running: worker spawned
    running --> running: step done (checkpoint)
    running --> waiting_approval: risky tool requested
    waiting_approval --> running: approve / reject
    running --> paused: pause
    paused --> running: resume
    running --> failed: LLM unreachable / bug
    failed --> running: resume (from checkpoint)
    running --> completed: finish_triage, fix applied and confidence >= 0.6
    running --> escalated: low confidence, nothing mitigated, step limit, token budget
    pending --> cancelled
    running --> cancelled
    paused --> cancelled
    waiting_approval --> cancelled
    failed --> cancelled
    completed --> [*]
    escalated --> [*]
    cancelled --> [*]
```
All transitions use `UPDATE runs SET status=? WHERE id=? AND status IN (...)`. If the row is not in an expected
state, nothing changes and the API returns **409**. Example race: operator presses *Pause* while the worker is
mid-step -> status becomes `paused`; the worker's checkpoint does not touch `status`; its later
`set_status('waiting_approval', expected=('running',))` is a no-op; the worker then sees `paused` and exits.

## 4. `Agent.step(run_id)` algorithm
The agent is *model + tools + guards*, in the spirit of LangChain's agent/middleware structure but with no framework
dependency. Each line below names the module that does the work.
```
rs = RunState.load(db.get_run(id)); if status != running: return STOP
if rs.state.pending:                                     # (0) approval.py + executor.py
    status = gate.decision(rs)
    pending      -> set waiting_approval; STOP
    approved     -> executor.run(tool); checkpoint + mark approval 'executed' IN ONE TRANSACTION
    rejected/cancelled -> gate.rejected_record(): observation + note "operator rejected: <comment>"
    gate.close(rs, record); CONTINUE
if guards.limit_reached(rs):                             # (1) guards.py: step limit, token budget
    finish(confidence=0) -> escalated
decision = planner.next_decision(rs, guidance)           # (2) planner.py
    = search_memory(services in scope) + build_context() + llm.decide()
    LLMError -> status failed (resumable); STOP
step += 1; add tokens/cost; events llm_call + thought; warning if guards.near_token_budget()
if no tool calls: note "always call a tool"; checkpoint; CONTINUE
for call in decision.calls:                              # (3) dispatch
    finish_triage -> report; approval.route(): completed (fix applied AND confidence>=0.6) else escalated;
                     promote memory if completed; stream + emit answer (last event); STOP
    update_plan   -> replace plan; event plan|plan_revised
    unknown tool  -> error observation
    risky         -> collect
    guards.is_repeated (3rd identical successful call) -> loop-guard error observation
    else          -> read list
executor.run_parallel(read list)  (max 4 threads) -> observations, flag injections, events
risky[1:] -> "one risky action per turn" error
risky[0]  -> gate.request(): approval row + state.pending; checkpoint; status waiting_approval; STOP
checkpointer.save(rs); CONTINUE                          # (4) state.py
```

### 4.1 Routing
`approval.route(confidence, mitigated, min_confidence)` decides the final status in code:

| Condition (checked in order) | Status | `routing_reason` |
|---|---|---|
| confidence < `MIN_CONFIDENCE` | `escalated` | `confidence 0.40 below 0.6` |
| no approved fix was applied | `escalated` | `no mitigation was applied; a human should confirm and close` |
| otherwise | `completed` | `mitigated and verified with high confidence` |

Step limit and token budget end the run through the same path with confidence 0, so they always escalate.
Only `completed` runs are promoted to long-term memory.

### 4.2 Error handling
| Error | Raised by | Handled as |
|---|---|---|
| `TransientToolError` | a tool (timeout, 5xx) | retried `TOOL_MAX_RETRIES` times with `backoff * 2^(n-1)`; then an error observation |
| `ToolError` | a tool or `validate_args()` | returned at once as an observation with hints; the model can correct itself |
| unknown tool, no tool call, repeated call | the loop | warning event and runtime note; the step counts toward the limit |
| `LLMError` | an LLM adapter after its own retries | event `error`, status `failed`, resumable from the checkpoint |
| any other exception in a step | `Runtime._drive()` | event `error`, status `failed`, resumable |
| `ConflictError` | `Runtime` on a disallowed transition | HTTP `409` |
| `KeyError` / missing run | API | HTTP `404` |

## 5. Runtime worker
```
_spawn(id):   with lock: if worker alive -> return; start thread _drive(id)
_drive(id):   loop:
                 with lock: if status != running: deregister; return     # atomic with _spawn
                 try: agent.step(id)  except Exception -> status failed, error event
                 sleep(STEP_DELAY)  # only so humans can watch; 0 in tests
recover():    for runs in (pending, running): event resumed_after_restart; _spawn
```

## 6. Sequence diagrams

### 6.1 Normal run with approval
```mermaid
sequenceDiagram
    participant UI
    participant API
    participant RT as Runtime
    participant AG as Agent.step
    participant LLM
    participant DB
    UI->>API: POST /api/runs {"scenario":"bad-deploy"}
    API->>RT: start_run
    RT->>DB: insert run, status running
    RT->>AG: worker loop
    AG->>LLM: context
    LLM-->>AG: update_plan
    AG->>DB: checkpoint, event plan
    AG->>LLM: context
    LLM-->>AG: get_metrics + get_logs + get_recent_deployments
    AG->>AG: run 3 tools in parallel (metrics retried once)
    AG->>DB: checkpoint, events tool_result/retry
    AG->>LLM: context
    LLM-->>AG: rollback_deployment(payments-api, v2.3.9)
    AG->>DB: approval pending, state.pending, status waiting_approval
    DB-->>UI: SSE approval_requested -> approval card
    UI->>API: POST /approval {"decision":"approve"}
    API->>RT: decide -> status running, spawn
    RT->>AG: step
    AG->>AG: execute rollback
    AG->>DB: checkpoint + approval executed (1 transaction)
    AG->>LLM: context -> get_metrics (verify) -> finish_triage
    AG->>DB: report, status completed, memory row
```

### 6.2 Rejection
```mermaid
sequenceDiagram
    participant UI
    participant RT as Runtime
    participant AG as Agent.step
    participant LLM
    UI->>RT: approval reject "not during peak"
    RT->>AG: step
    AG->>AG: observation: Operator rejected ... + runtime note
    AG->>LLM: context includes the rejection note
    LLM-->>AG: finish_triage(confidence 0.5) or another safe action
    AG-->>UI: status escalated (handed to human)
```

### 6.3 Resume after restart
```mermaid
sequenceDiagram
    participant P1 as Server process 1
    participant DB
    participant P2 as Server process 2
    P1->>DB: checkpoint step 3 (status running)
    Note over P1: crash / Ctrl+C during step 4
    P2->>DB: startup: recover() - runs with status running
    P2->>DB: event resumed_after_restart
    P2->>DB: load checkpoint (step 3)
    P2->>P2: redo step 4 from scratch (reads are idempotent)
    Note over P2,DB: if the run was waiting_approval it simply stays waiting - the operator approves later
```

## 7. API contract
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| GET | `/health` | - | `{status, llm_mode, model}` | |
| GET | `/api/scenarios` | - | list of `{id,title,alert}` | |
| GET | `/api/tools` | - | list of `{name,description,parameters,risky}` | |
| GET | `/api/memory` | - | list of memory rows | |
| POST | `/api/runs` | `{scenario, alert?}` (`alert` replaces the scenario's alert text) | 201 run view | 400 unknown scenario |
| GET | `/api/runs` | - | list (summary) | |
| GET | `/api/runs/{id}` | - | run view (status, plan, steps, notes, pending_approval, approvals, report, answer, sources, done, not_done, infra, tokens, cost) | 404 |
| GET | `/api/runs/{id}/events?after=N` | - | events with id > N | 404 |
| GET | `/api/runs/{id}/stream?after=N` | - | `text/event-stream` (`event: token`, `event: trace`, final `event: end`) | 404 |
| POST | `/api/runs/{id}/pause` | - | run view | 404, 409 |
| POST | `/api/runs/{id}/resume` | - | run view | 404, 409 |
| POST | `/api/runs/{id}/cancel` | - | run view | 404, 409 |
| POST | `/api/runs/{id}/approval` | `{decision: approve|reject, comment}` | approval row | 404, 409, 422 |
| POST | `/api/runs/{id}/guidance` | `{message}` | `{ok:true}` | 404, 409 (finished) |
curl for each: [API.md](API.md).

## 8. Tools
| Tool | Type | Args | Notes |
|---|---|---|---|
| `update_plan` | control | `steps[]` | handled by loop |
| `list_services` | read | - | status, version, replicas |
| `get_metrics` | read | `service` | current vs baseline; flaky once in bad-deploy |
| `get_logs` | read | `service, limit?` | untrusted content |
| `get_recent_deployments` | read | `service?` | newest first |
| `search_runbooks` | read | `query` | keyword scoring, top 2 |
| `restart_service` | **risky** | `service` | approval |
| `rollback_deployment` | **risky** | `service, to_version` | validates target version |
| `scale_service` | **risky** | `service, replicas (1-20)` | approval |
| `finish_triage` | control | `root_cause, severity, confidence, affected_services?, actions_taken?, follow_ups?` | ends run |

Retry policy: only `TransientToolError` is retried, `TOOL_MAX_RETRIES=3`, delay `backoff * 2^(n-1)`.
`ToolError` (bad input) is returned immediately with a helpful message.

## 9. LLM adapters
- **Gemini** (`google-genai`): `client.models.generate_content_stream(model, contents=prompt, config)` with
  `system_instruction`, `tools=[Tool(function_declarations=[FunctionDeclaration(name, description, parameters_json_schema)])]`,
  `FunctionCallingConfig(mode=AUTO)`, automatic function calling disabled (the loop executes tools itself),
  temperature 0.1. Merges the streamed chunks (`collect_stream()`), then parses text parts as the *thought* and all `function_call` parts as (parallel) tool calls;
  usage from `usage_metadata` (prompt, candidates + thoughts tokens). Retries 4x; on 429 waits the server's hint (cap 60 s).
  API version defaults to `v1beta` (override `GEMINI_API_VERSION`). Model `GEMINI_MODEL` (default `gemini-3.5-flash-lite`).
- **Mock**: rule-based policy reading the structured state; deterministic; token counts estimated as chars/4.

## 10. Configuration
See `.env.example`: `GEMINI_API_KEY, GEMINI_MODEL, DB_PATH, MAX_STEPS, MAX_RUN_TOKENS, STEP_DELAY_SECONDS,
STREAM_DELAY_SECONDS, TOOL_MAX_RETRIES, TOOL_BACKOFF_SECONDS, MIN_CONFIDENCE, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M`.

## 11. Tests (what proves what)
| Test file (in `tests/`) | Proves |
|---|---|
| `tools/test_tools.py` | schemas, validation, transient retry with exponential backoff, permanent errors not retried |
| `agent/test_loop.py` | full plan->tools->approval->verify->report; reject path; low-confidence routing; injection cannot bypass gate; memory recall; guidance |
| `agent/test_resume.py` | restart while waiting approval; crash mid-run resumes from checkpoint; approved action exactly once across crash; pause/resume/cancel |
| `agent/test_guards_and_recovery.py` | step limit + loop guard; token budget; LLM outage -> failed -> resume; self-correction after bad args; unknown tool; one risky per turn |
| `agent/test_context.py` | truncation, compaction never drops a step, tag-escape safety |
| `llm/test_gemini_adapter.py` | Gemini response parsing, declarations, retries, 429 hint (SDK faked - no key needed) |
| `agent/test_plan_answer.py` | plan before any other tool call; only `update_plan` offered until a plan exists; answer is the last event and matches the report; escalated answer; plan and answer survive a restart |
| `agent/test_streaming.py` | deltas join to the saved thought, plan and answer text; plan deltas before answer deltas; Gemini stream merging |
| `agent/test_sources.py` | sources, done and not done for a completed run, a rejected action, and a run with nothing wrong |
| `api/test_api.py` | every endpoint incl. SSE, 404/409/422, pause really stops, UI page served |

## 12. Framework choices
Frameworks considered, and why each was or was not adopted. The rule: adopt only what keeps behaviour identical and
adds no build step.

| Option | What it offers | Used here | Why |
|---|---|---|---|
| LangGraph (`create_agent`, Deep Agents) | Graph runtime, checkpointer, `interrupt()` | No; same structure in plain Python | A rewrite would change the loop the tests and traces pin down. |
| LangSmith / OpenTelemetry | Hosted tracing, monitoring and evals | No; own events table + `evals/` harness | Works offline with no account. Events map to OTel GenAI spans later. |
| React, Tailwind, shadcn | Component model, utility CSS | No; plain HTML, CSS and JS | No build step; design tokens, states and motion are plain CSS. |
| (FastAPI, already in use) | Pydantic models | Yes, for request bodies (`StartRun`, `Decision`, `Guidance`) and `Answer` | Run state stays a plain dict in SQLite JSON; typing it with Pydantic would change serialisation. `RunState` is a dataclass. |

**How the loop maps to LangGraph:** `planner.next_decision` = model node; `executor.run_parallel` = tool node;
`ApprovalGate.request` = `interrupt()` before risky tools, `decide` = resume with a command; `Checkpointer` + SQLite =
LangGraph checkpointer (one checkpoint per super-step); `guards` = recursion limit and middleware; `Runtime.recover()` =
resuming a thread from its last checkpoint.

**Libraries in use:** Python: `fastapi`, `uvicorn`, `pydantic`, `google-genai`, `python-dotenv`; tests: `pytest`, `httpx`.
Browser: none (plain HTML, CSS and JS in `frontend/`, no CDN).

