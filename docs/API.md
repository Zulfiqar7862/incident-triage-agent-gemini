# API reference + curl examples

Base URL `http://localhost:8000`. Interactive OpenAPI docs: http://localhost:8000/docs.
All bodies are JSON. Errors: `400` bad input, `404` unknown run, `409` action not allowed in the run's
current state (e.g. approving when nothing is pending), `422` validation error.

```bash
BASE=http://localhost:8000
```

## Meta
```bash
curl -s $BASE/health                 # {"status":"ok","llm_mode":"gemini|mock","model":"..."}
curl -s $BASE/api/scenarios          # the 4 simulated incidents
curl -s $BASE/api/tools              # tool schemas (JSON schema) + risky flag
curl -s $BASE/api/memory             # long-term memory (past incidents)
```

## Runs
```bash
# start a run (scenario: bad-deploy | memory-leak | traffic-spike | ghost-alert; alert text optional)
RUN=$(curl -s -X POST $BASE/api/runs -H 'Content-Type: application/json' \
      -d '{"scenario":"bad-deploy"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s $BASE/api/runs                       # list runs (newest first)
curl -s $BASE/api/runs/$RUN                  # full state: status, plan, steps, pending_approval, report, infra, tokens, cost
curl -s "$BASE/api/runs/$RUN/events?after=0" # trace events (poll with ?after=<last id>)
curl -N $BASE/api/runs/$RUN/stream           # Server-Sent Events: live trace (Ctrl+C to stop)
```

## Human-in-the-loop
```bash
# approve or reject the pending risky action (only when status = waiting_approval)
curl -s -X POST $BASE/api/runs/$RUN/approval -H 'Content-Type: application/json' \
     -d '{"decision":"approve","comment":"ok, roll back"}'
curl -s -X POST $BASE/api/runs/$RUN/approval -H 'Content-Type: application/json' \
     -d '{"decision":"reject","comment":"not during peak hours"}'

# guidance: a message the agent reads at its next step (trusted, high priority)
curl -s -X POST $BASE/api/runs/$RUN/guidance -H 'Content-Type: application/json' \
     -d '{"message":"Prefer rollback over restart"}'
```

## Lifecycle
```bash
curl -s -X POST $BASE/api/runs/$RUN/pause    # running -> paused (stops at the next step boundary)
curl -s -X POST $BASE/api/runs/$RUN/resume   # paused|failed -> running (continues from last checkpoint)
curl -s -X POST $BASE/api/runs/$RUN/cancel   # any non-final -> cancelled (pending approval is cancelled too)
```

## Restart-resume check (terminal)
```bash
# 1. start a run, wait until status is waiting_approval (or running)
# 2. stop the server (Ctrl+C) and start it again: make run
curl -s $BASE/api/runs/$RUN | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["step"])'
# 3. approve -> the run finishes; the trace shows "resumed_after_restart" for runs that were mid-step
```

## Run object (abridged)
```json
{
  "id": "a1b2c3d4e5", "scenario": "bad-deploy", "status": "waiting_approval", "step": 5,
  "plan": ["List services...", "..."], "tokens_in": 6100, "tokens_out": 420, "cost_usd": 0.0029,
  "pending_approval": {"id": "f00", "tool": "rollback_deployment",
                       "args": {"service": "payments-api", "to_version": "v2.3.9"}, "reason": "...", "status": "pending"},
  "steps": [{"n": 1, "thought": "...", "calls": [{"tool": "update_plan", "ok": true, "summary": "..."}]}],
  "report": null, "infra": {"payments-api": {"status": "degraded", "version": "v2.4.0", "replicas": 3}}
}
```
