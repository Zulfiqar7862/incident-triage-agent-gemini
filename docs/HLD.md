# High-level design: Incident Triage Agent

## 1. Problem and goals
When an alert fires, an on-call engineer repeats the same loop: find the sick service, read metrics and
logs, check what changed, find the runbook, apply a fix, verify. It is slow at 3 a.m. and error-prone.
**Goal:** an agent that does this loop on its own, **never changes production without a human**, shows its plan
and reasoning live, and **does not lose work** if the server restarts.

**Non-goals:** real infrastructure access, multi-tenant auth, horizontal scaling. Section 12 covers how each would be added.

### Functional requirements
| # | Requirement | Where |
|---|---|---|
| F1 | Plan, then act step by step with tools; revise the plan when evidence changes | `agent/loop.py`, `agent/planner.py`, `update_plan` tool |
| F2 | Recover from tool errors (retry transient, self-correct on permanent) | `tools/registry.py`, loop notes |
| F3 | Human approval for every mutating action; approve/reject with comment | `agent/approval.py`, `/approval` API, UI card |
| F4 | Start / pause / resume / cancel runs | `agent/runtime.py` |
| F5 | Resume after process restart from the last checkpoint | `db.save_checkpoint`, `Runtime.recover()` |
| F6 | Operator sees plan, thoughts, tool calls, results, tokens, cost, latency live; model text streams as it is written | events table, `agent/tokens.py`, SSE, UI |
| F7 | Operator can send guidance mid-run | `/guidance` API, context layer |
| F8 | Structured final report + routing to a human when confidence < 0.6 or nothing was mitigated | `finish_triage`, `approval.route()` |
| F9 | Long-term memory of past incidents | `memory` table |
| F10 | A final answer with sources and a done / not-done list | `agent/answer.py`, `agent/sources.py`, `answer` event |

### Non-functional requirements
- Runs locally with one command, no cloud, no Docker; works offline (mock LLM).
- Simple enough to explain end to end; every concept maps to production tools.
- Safe by construction: approval rule in code; tool output treated as untrusted data.
- Observable: every decision is in the trace with tokens / latency / cost.

## 2. Architecture

```mermaid
flowchart LR
    OP[Operator<br/>browser] -- "HTTP JSON + SSE" --> API
    subgraph Server["FastAPI process (api/main.py)"]
        API[REST API + SSE<br/>+ static UI]
        RT[Runtime<br/>lifecycle + worker threads]
        AG[Agent.step<br/>thin step loop]
        PL[Planner<br/>memory + context + model]
        EX[Executor<br/>tools, retry, parallel reads]
        GT[Approval gate + guards<br/>risk policy, routing, limits]
        CTX[Context builder<br/>budget + compaction]
        TR[Tool registry<br/>schemas, validation]
        LLM[LLM adapter<br/>Gemini or Mock]
        API --> RT --> AG
        AG --> PL --> CTX
        PL --> LLM
        AG --> EX --> TR
        AG --> GT
        AG -. text deltas .-> TS[TokenStream<br/>in memory]
        TS -.-> API
    end
    LLM -- "function calling" --> GEM[(Google Gemini API)]
    TR --> SIM[Simulated infra<br/>services, metrics, logs,<br/>deploys, runbooks]
    AG <--> DB[(SQLite<br/>runs / events /<br/>approvals / memory)]
    API <--> DB
```

### Components
| Component | Responsibility | Key idea |
|---|---|---|
| **UI** (`frontend/`) | One conversation column per run: incident, plan (always visible), streamed thinking, acting rows with an inline approval card, the answer, sources and done / not done; composer to start a run or message the agent; pause / resume / cancel; empty, loading and error states | Plain JS; SSE for deltas and events, polling for run state |
| **API** (`src/incident_triage_agent/api/main.py`) | REST endpoints for runtime, tools, state; SSE stream | Thin: validates input, calls Runtime, returns DB state |
| **Runtime** (`agent/runtime.py`) | Lifecycle of runs; one worker thread per active run; `recover()` on startup | Control = conditional status change in DB; worker notices at next step |
| **Agent** (`agent/loop.py`) | Executes exactly one step and checkpoints; a thin loop over the parts below | Stateless between steps: always starts from DB |
| **Token stream** (`agent/tokens.py`) | Per-run in-memory buffer of text deltas (plan, thought, answer) | Never written to SQLite; the final text is saved once as a normal event |
| **Answer and sources** (`agent/answer.py`, `agent/sources.py`) | Build the final answer from the report; derive sources and done / not done from step records | Pure functions over saved state; no new state |
| **Planner** (`agent/planner.py`) | Recalls memory, builds the context, asks the model for the next decision | The only place that talks to the model |
| **Executor** (`agent/executor.py`) | Runs tool calls: schema validation, retry with backoff, parallel reads, injection flag | The only place that runs tools |
| **Approval gate** (`agent/approval.py`) | Turns a risky call into a pending approval; routes a finished run to `completed` or a human | Enforced in code, not in the prompt |
| **Guards** (`agent/guards.py`) | Step limit, token budget, loop guard | Hard limits the model cannot override |
| **State** (`agent/state.py`) | `RunState` and `Checkpointer` | One write per step = durable execution |
| **Context builder** (`agent/context.py`) | Builds the prompt from state in layers within a budget | Truncation + compaction, nothing silently dropped |
| **LLM adapters** (`llm/`) | `decide(context, on_text) -> thought + tool calls + usage` | Gemini (streamed function calling) or deterministic mock |
| **Tool registry** (`tools/`) | JSON-schema tools, validation, retry/backoff, risky flag | Errors become observations, never crashes |
| **Simulated infra** (`sim/world.py`) | 4 services, 4 scenarios | World state lives inside the checkpoint |
| **SQLite** (`db.py`) | Checkpoints, trace, approvals, long-term memory | WAL mode, transactions, conditional updates |

## 3. How a run flows (big picture)

```mermaid
sequenceDiagram
    participant O as Operator (UI)
    participant A as API
    participant R as Runtime worker
    participant L as LLM (Gemini)
    participant T as Tools
    participant D as SQLite
    O->>A: POST /api/runs {scenario}
    A->>D: create run (pending -> running)
    A->>R: spawn worker
    loop each step
        R->>D: load checkpoint
        R->>L: context (incident, plan, memory, guidance, history)
        L-->>R: text chunks (streamed)
        R-->>O: SSE token deltas (memory only)
        L-->>R: tool calls + usage
        R->>T: read-only tools (parallel, retry)
        T-->>R: observations
        R->>D: checkpoint + trace events
        D-->>O: SSE events (live)
    end
    R->>D: approval pending, status=waiting_approval
    O->>A: POST /approval {approve}
    A->>R: spawn worker
    R->>T: execute approved action
    R->>D: checkpoint + mark approval executed (one transaction)
    R->>L: verify / finish
    R->>D: report, status=completed|escalated, save memory, answer event
    D-->>O: SSE answer -> result, sources, done / not done
```

The first step may only call `update_plan`, so the plan is always the first thing the operator sees. The answer is
always the last event of a finished run.

## 4. Design decisions and trade-offs

| Decision | Chosen | Alternative | Why / trade-off |
|---|---|---|---|
| Orchestration | Own small loop (`agent/`: planner, executor, approval gate, guards, state) | LangGraph, Google ADK, Temporal | Small enough to explain end to end. Same concepts (checkpoint per node, interrupt for approval, resume). Would adopt LangGraph/ADK/Temporal for multi-worker production. |
| Conversation state | Rebuild prompt from DB each step | Keep growing chat history in the SDK | Small, deliberate context; resume is trivial; no provider-specific history (e.g. thought signatures). Cost: we re-send the summary every step (cheap at these sizes). |
| Durability | SQLite checkpoint after every step | Event sourcing, Postgres, Temporal | Zero setup, transactional. Single process only - documented as next step. |
| Delivery semantics | Read tools at-least-once; approved actions exactly-once | Exactly-once everywhere | Reads are idempotent, so repeating after a crash is harmless. Writes are protected by marking the approval `executed` in the same transaction as the checkpoint (real infra: approval id as idempotency key). |
| Safety | `risky` flag enforced in code | Ask the model to ask for approval | The model can be tricked; the code cannot. |
| HITL routing | Approval for all writes + confidence threshold | Auto-approve low-risk writes | Simple and safe for a first version. Next: policy per tool / environment. |
| Retrieval | Keyword top-2 runbooks | Embeddings + vector DB | 4 runbooks: keywords are more precise and explainable. Vector search pays off at hundreds of docs. |
| Concurrency | Thread per active run | asyncio / job queue | Gemini SDK + sqlite3 are sync; threads are simplest. Queue + leases when scaling out. |
| UI | Plain HTML/JS + SSE | React | No build step, opens anywhere; operator UX matters more than the framework. |
| Streaming | In-memory deltas, final text saved once | Persist every delta | No per-token writes; a late or reconnecting client still gets the full saved text. Cost: deltas are lost on restart (the saved events are not). |

## 5. State and recovery
- **What is saved:** after every step, one `UPDATE` writes the checkpoint (`runs.state`: world, step records, notes,
  pending action) together with plan, step counter, tokens and cost. Trace events are appended alongside.
- **What is not saved:** token deltas. They are a view of text that is saved anyway once the step finishes.
- **Control:** pause, resume, cancel and approve are conditional status updates
  (`UPDATE ... WHERE status IN (...)`). The worker checks the status before each step, so a pause takes effect at
  the next step boundary and races resolve to a single winner (the loser gets `409`).
- **Recovery:** on startup `Runtime.recover()` finds runs in `pending` or `running`, logs `resumed_after_restart`,
  and starts a worker that redoes the interrupted step from the last checkpoint. Reads are idempotent, so repeating
  them is safe. Runs in `waiting_approval` simply keep waiting.
- **Exactly once for changes:** an approved action and the checkpoint that records it commit in one transaction
  that also marks the approval `executed`. A crash before the commit re-runs the action once on resume; a crash after
  it never repeats it.
- **Model outage:** the run becomes `failed` with the error shown; the operator resumes it from the checkpoint.

## 6. Safety and human in the loop
- Every tool has a `risky` flag in code. A risky call never runs directly: the gate creates a pending approval, sets
  `waiting_approval`, and stops. Only `POST /approval` moves it on. One risky action per turn.
- The operator sees the action in plain words, what each argument does, the agent's reason, and the evidence so far.
  A rejection and its comment become a runtime note, so the agent adapts or hands over instead of retrying.
- Routing is in code: `completed` needs an applied fix and confidence ≥ `MIN_CONFIDENCE` (0.6). Everything else,
  including step limit and token budget, is `escalated` to a human.
- Tool output is untrusted: it is truncated, wrapped as data, and scanned for injection patterns. A flagged result
  produces a warning. Injected text cannot skip approval, because the risky check is code, not a prompt rule.
- Guidance from the operator is trusted and has its own layer in the prompt.

## 7. Context engineering (what the model sees, and why)
Rebuilt every step, in priority order: **incident** -> **similar past incidents** (max 3, marked "may be
outdated") -> **plan** -> **operator guidance** (trusted) -> **work so far** (last 4 steps full, older steps
1-line summaries) -> **runtime notes** (errors, rejections, loop-guard) -> **task**.
Budgets: tool output <= 1,500 chars; whole prompt <= 24,000 chars (~6k tokens). If over budget, fewer steps are
shown in full. Per-layer token estimates are logged on every `llm_call` event so you can *see* the budget.
Why small: models get worse as input grows, long before the advertised limit; more context != better reasoning.

## 8. Memory
- **Short-term (working) memory** = the run checkpoint: plan, steps (calls + results), notes, pending action, world.
- **Long-term memory** = `memory` table: one row per affected service of a *confident, completed* run
  (symptom, root cause, resolution). Promotion rule: only `completed` runs (fix applied and confidence >= 0.6) are saved.
  **Redaction** (secrets, emails) before saving. **Staleness**: only memories < 90 days are recalled, max 3,
  and the prompt says "may be outdated - verify".

## 9. Failure modes and how they are handled
| Failure | Handling |
|---|---|
| Tool timeout / 5xx | Retry with exponential backoff (0.3, 0.6, 1.2 s); then error observation to the model |
| Bad tool arguments / unknown service | Validation error returned as observation with hints ("Known services: ..."); model self-corrects |
| Model calls unknown tool / replies without a tool | Observation / runtime note, step counted, loop continues |
| Model loops on the same call | Loop guard blocks a 3rd identical successful call; step limit (15) ends run as `escalated` |
| Token budget exhausted | Run ends `escalated`; 80% budget warning in trace |
| Gemini 429 / 5xx / network | Retries honouring server `retry in Ns`; then run `failed` - **resumable** from checkpoint |
| Server crash mid-step | `recover()` on startup re-runs the step from the last checkpoint |
| Crash after approval, before execution | Approval is still `approved` -> executed on resume, exactly once |
| Prompt injection in logs | Flagged + wrapped as untrusted data; cannot bypass the approval gate |
| Agent finds nothing / unsure | Low confidence -> `escalated` to human, no invented fix |
| Operator rejects action | Rejection + comment fed back; agent adapts or escalates |

## 10. Observability
Every step writes events: `llm_call` (model, tokens in/out, latency, cost, context layer sizes, memories used),
`thought`, `plan`, `tool_call`, `tool_result` (ok, attempts, latency, output), `retry`, `warning`,
`approval_requested/decided`, `guidance`, `status`, `error`, `run_finished`, `memory_saved`, `answer`, `resumed_after_restart`.
Live text arrives separately as SSE `token` events (plan, thought, answer).
Run totals (tokens, cost) are on the run row. **Production next step:** emit the same data as OpenTelemetry
GenAI spans (run = trace, step = span, LLM/tool = child spans) to Langfuse / Jaeger, with budget alerts.

## 11. Evaluation
`make eval` runs 4 golden scenarios end to end with an automatic operator (approves only the expected action)
and scores: correct status, correct action proposed, root cause keywords, within step budget, no unapproved
change. Results saved to `evals/results/`. Run before/after any prompt or model change.

## 12. Scaling path (what changes in production)
1. SQLite -> Postgres; worker threads -> job queue workers with a lease per run (or Temporal / LangGraph checkpointer);
   the in-memory token stream -> Redis pub/sub so any API instance can stream any run.
2. Simulated tools -> real adapters (K8s, Prometheus, Loki, ArgoCD) with least-privilege credentials.
3. SSO + RBAC on approvals, approval expiry, audit log export.
4. OTel tracing, dashboards + alerts on cost/latency/failure rate per agent and per tool.
5. Vector retrieval over runbooks + incident history; LLM-as-judge evals in CI.
