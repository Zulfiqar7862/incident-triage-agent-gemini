# Incident Triage Agent

A small **governed operator control plane** for incident triage: an AI agent that **triages production incidents on its own** - it plans, calls tools, recovers
from errors, and **stops for human approval before touching production**. An operator watches every
thought and tool call live in a web UI and can approve, reject, pause, resume, cancel or give guidance.
Every step is checkpointed to SQLite, so a run **survives a server restart** and continues where it stopped.

- LLM: **Google Gemini** (official `google-genai` SDK, function calling). No key? It runs with a
  deterministic offline "mock" policy, so everything works (and is testable) without internet.
- Backend: **Python + FastAPI**. UI: one plain HTML/JS page served by FastAPI (no Node, no build step).
- Infra is **simulated** (4 services with metrics, logs, deployments, runbooks) so it runs on a laptop.

```
git clone <repo-url> incident-triage-agent
cd incident-triage-agent
make install       # creates .venv and installs requirements
make quickstart    # creates .env from .env.example (if missing) and starts http://localhost:8000
```
Put your key in `.env` (`GEMINI_API_KEY=...`) to use Gemini; leave it empty for offline mock mode.

---

## 1. Why incident triage? (use-case rationale)

- It is the platform task where an agent gives the most value: on-call engineers spend the first
  15-30 minutes of every incident doing the same loop - *which service is sick? metrics? logs? what changed?
  which runbook?* - before they can act.
- It **needs real autonomy**: the next tool depends on what the last tool returned (not a fixed pipeline).
- It **needs human-in-the-loop**: reading is safe, but rollback / restart / scale change production.
  This is exactly where an agent must stop and ask.
- It is easy to **evaluate**: each scenario has a known root cause and a known correct fix.

## 2. What it does (one run)

1. Reads the alert, writes a **plan** (`update_plan` tool).
2. `list_services` -> finds the unhealthy service.
3. Calls `get_metrics`, `get_logs`, `get_recent_deployments` **in parallel**.
   (`get_metrics` times out once in the `bad-deploy` scenario -> **retry with backoff** is visible in the trace.)
4. `search_runbooks` (small retrieval over `runbooks/*.md`, top-2 only).
5. Proposes a fix, e.g. `rollback_deployment(payments-api, v2.3.9)` -> **run pauses, operator approves/rejects**.
6. Verifies with fresh metrics, then `finish_triage` with a structured report and a **confidence** score.
   It is `completed` only if a human-approved fix was applied **and** confidence >= 0.6; otherwise it is
   `escalated` (handed to a human). This rule is in code, not left to the model.
7. Confident, finished incidents are saved to **long-term memory** and recalled in later runs.

Scenarios: `bad-deploy` (rollback), `memory-leak` (restart), `traffic-spike` (scale; its logs contain a
**prompt-injection** line that tries to make the agent roll back everything), `ghost-alert` (nothing is wrong -
the agent must *not* invent a fix).

## 3. Run it

### Terminal
| Command | What it does |
|---|---|
| `make install` | create `.venv`, install `requirements.txt` (needs Python 3.11+) |
| `make quickstart` | create `.env` if missing, print URLs, start server on :8000 |
| `make run` | start server (no .env creation) |
| `make test` | 34 pytest tests, offline (forces the mock LLM even if a key is set) |
| `make eval` | golden-scenario eval (uses Gemini if key set, else mock) |
| `make demo` | full run through **curl only** (start server first, in another terminal) |
| `make clean` | delete `data/` (the SQLite database) and caches |

Windows without `make`: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`,
`copy .env.example .env`, `.venv\Scripts\uvicorn app.main:app --port 8000`.

### PyCharm
1. *File > Open* the folder. *Settings > Project > Python Interpreter > Add > Existing* -> `.venv/bin/python`
   (run `make install` first, or let PyCharm create the venv and install `requirements.txt`).
2. Two shared run configurations are included in `.run/`: **Agent Server** (uvicorn with `.env`) and
   **All Tests** (pytest). They appear in the run dropdown automatically.
3. Run **Agent Server**, open http://localhost:8000. Use the PyCharm terminal for `make demo` / curl.

### Try the durability yourself
```bash
make quickstart                     # terminal 1
make demo                           # terminal 2 -> stops at approval... or start a run in the UI
# Ctrl+C the server while a run is "running" or "waiting_approval", then:
make quickstart                     # it prints "resumed N interrupted run(s)" and continues
```
All curl commands for every endpoint: [docs/API.md](docs/API.md).

## 4. Design decisions (the short version)

| Decision | Why |
|---|---|
| **Own small loop, no framework** (no LangGraph/ADK) | 2-4h timebox; every line is explainable; the concepts (checkpoint, interrupt, resume) map 1:1 to LangGraph/ADK if the team uses one. |
| **Prompt rebuilt from DB state each step** (no long chat history) | Context is designed, not accumulated. Resume is trivial: nothing lives only in memory. |
| **Checkpoint to SQLite after every step** | Durable execution with zero infra. A crash loses at most the step in flight; read tools are safe to repeat. |
| **Approval gate enforced in code** (`risky=True` tools) | Prompt injection can change what the model *asks* for, never what *runs*. |
| **Approved action + checkpoint in one transaction** | An approved production change runs exactly once, even across a crash. |
| **Context budget**: truncate tool output, compact old steps to 1-line summaries | Long runs stay coherent; nothing is silently dropped (every step keeps its summary). |
| **Confidence-based routing (in code)** | Low confidence or "nothing mitigated" goes to a human (`escalated`). Found by the live Gemini eval: on `ghost-alert` Gemini was *confident* nothing was wrong - correct, but a human should still close it. |
| **Offline mock policy** | Deterministic tests/evals, demo without a key, and CI never calls a paid API. |
| **Threads + SQLite status flags** for lifecycle | Simple. Pause/cancel are checked at step boundaries; conditional `UPDATE ... WHERE status=...` prevents races. |

Full detail: [docs/HLD.md](docs/HLD.md) (architecture) and [docs/LLD.md](docs/LLD.md) (schema, API, state machine, sequences).

## 5. Folder structure
```
incident-triage-agent/
├── app/
│   ├── main.py              # FastAPI app: REST + SSE API, serves the UI
│   ├── config.py            # all settings (env / .env)
│   ├── db.py                # SQLite: runs (checkpoints), events (trace), approvals, memory
│   ├── agent/
│   │   ├── loop.py          # Agent.step(): ONE step = context -> LLM -> tools/approval -> checkpoint
│   │   ├── runtime.py       # lifecycle: start/pause/resume/cancel/approve, worker threads, recover()
│   │   ├── context.py       # context engineering: layers, truncation, compaction, budget
│   │   ├── prompts.py       # system prompt
│   │   └── safety.py        # injection detection, redaction
│   ├── llm/
│   │   ├── base.py          # LLM interface (decide -> thought + tool calls + usage)
│   │   ├── gemini.py        # Gemini via google-genai, function calling, retries (429-aware)
│   │   └── mock.py          # deterministic offline policy
│   ├── tools/
│   │   ├── registry.py      # Tool schema, validation, retry with exponential backoff
│   │   └── infra_tools.py   # the 10 tools (5 read, 3 risky, 2 control)
│   └── sim/world.py         # simulated infrastructure + 4 scenarios
├── static/                  # index.html, app.js, style.css (operator UI)
├── runbooks/                # markdown runbooks used by search_runbooks
├── tests/                   # pytest: loop, approvals, resume, guards, context, Gemini adapter, API
├── evals/                   # golden.json + run_evals.py (scored regression eval)
├── scripts/                 # demo.sh (curl walkthrough), check_python.py
├── docs/                    # HLD.md, LLD.md, API.md, INTERVIEW_PREP.md
├── .run/                    # PyCharm run configurations
├── Makefile, requirements.txt, .env.example, AI_LOG.md
```

## 6. Verified results
- `make test`: **34 passed** (offline, ~4 s).
- `make eval` with the mock LLM: **100%** on 4 golden scenarios.
- `make eval` with **live Gemini `gemini-3.5-flash-lite`**: first run 85% -> two real issues found and fixed:
  1. the free tier allows **15 requests/min**; one run hit 429 and went to `failed` (resumable, as designed) -> the
     client now waits for the server's `retry in Ns` hint;
  2. on `ghost-alert` Gemini was *confident* nothing was wrong and finished as `completed` -> routing rule moved into
     code (completed only if a fix was applied).
  After the fixes: **100%** (4/4), ~5-9 steps and ~$0.005 per run.

## 7. What's next (with more time)
1. **Real adapters** behind the same tool interface: Kubernetes API, Prometheus, Loki, ArgoCD (read-only service account first).
2. **Postgres + a job queue** (or Temporal / LangGraph checkpointer) for multiple workers; lease per run instead of in-process threads.
3. **OpenTelemetry GenAI spans** exported to Langfuse/Jaeger (the event table already has the data: tokens, latency, cost per step).
4. **Vector retrieval** for runbooks + past incidents (embeddings) once there are hundreds of them; today keyword top-2 is more precise.
5. **LLM-as-judge** + human review in the eval harness, run in CI on every prompt/model change.
6. **AuthN/Z + audit**: who approved what (SSO), per-tool RBAC, approval expiry, two-person rule for SEV1 actions.
7. Supervisor/worker agents (e.g. a log-analysis sub-agent) only when a single agent's context gets too crowded.

## 8. AI usage
AI tools were used to build this (required by the brief). See [AI_LOG.md](AI_LOG.md).
