#!/usr/bin/env bash
# End-to-end demo with curl only. Start the server first (make quickstart), then: make demo
# Usage: bash scripts/demo.sh [scenario] [base_url]
set -euo pipefail
SCENARIO="${1:-bad-deploy}"
BASE="${2:-http://localhost:8000}"
PY="$(dirname "$0")/../.venv/bin/python"; [ -x "$PY" ] || PY=python3
field() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d$1)"; }

echo "== health";            curl -s "$BASE/health"; echo
echo "== start run ($SCENARIO)"
RUN=$(curl -s -X POST "$BASE/api/runs" -H 'Content-Type: application/json' -d "{\"scenario\":\"$SCENARIO\"}" | field "['id']")
echo "run id: $RUN"

echo "== waiting for the agent to need approval (or finish)"
for i in $(seq 1 60); do
  STATUS=$(curl -s "$BASE/api/runs/$RUN" | field "['status']")
  [ "$STATUS" != "running" ] && break; sleep 1
done
echo "status: $STATUS"

if [ "$STATUS" = "waiting_approval" ]; then
  echo "== pending action:"
  curl -s "$BASE/api/runs/$RUN" | field "['pending_approval']['tool'], '->', d['pending_approval']['args']"
  echo "== approving"
  curl -s -X POST "$BASE/api/runs/$RUN/approval" -H 'Content-Type: application/json' \
       -d '{"decision":"approve","comment":"approved from demo script"}' | field "['status']"
  for i in $(seq 1 60); do
    STATUS=$(curl -s "$BASE/api/runs/$RUN" | field "['status']")
    [ "$STATUS" != "running" ] && break; sleep 1
  done
fi

echo "== final status: $STATUS"
curl -s "$BASE/api/runs/$RUN" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['report'], indent=2)); print('steps', d['step'], 'tokens', d['tokens_in']+d['tokens_out'], 'cost \$', d['cost_usd'])"
echo "== trace (event types)"
curl -s "$BASE/api/runs/$RUN/events" | "$PY" -c "import sys,json; print(' > '.join(e['type'] for e in json.load(sys.stdin)))"
echo "Open the UI: $BASE/?run=$RUN"
