"""Golden-set eval harness: run every scenario end-to-end and score the agent.

Usage:  make eval            (mock LLM if no key, Gemini if GEMINI_API_KEY is set)
Run it before/after changing the prompt or the model to catch regressions.

The "operator" here is automatic: it approves an action only if it matches the
expected one, otherwise it rejects (so a wrong action costs points, never changes infra).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from incident_triage_agent.agent.loop import Agent
from incident_triage_agent.agent.runtime import Runtime
from incident_triage_agent.config import Settings
from incident_triage_agent.db import DB
from incident_triage_agent.llm import make_llm
from incident_triage_agent.tools.infra_tools import build_registry

HERE = Path(__file__).parent


def score_case(case: dict, rt: Runtime, db: DB) -> dict:
    run = rt.wait(rt.start_run(case["scenario"])["id"], timeout=180)
    proposed = None
    for _ in range(3):  # at most a few approval rounds
        if run["status"] != "waiting_approval":
            break
        appr = db.pending_approval(run["id"])
        proposed = proposed or {"tool": appr["tool"], "args": appr["args"]}
        ok = appr["tool"] in case["expect_action"] and all(
            appr["args"].get(k) == v for k, v in case["expect_args"].items())
        rt.decide(run["id"], ok, "eval auto-operator")
        run = rt.wait(run["id"], timeout=180)
    report = run.get("report") or {}
    checks = {
        "status": run["status"] == case["expect_status"],
        "action": ((proposed or {}).get("tool") in case["expect_action"]) if case["expect_action"]
                  else proposed is None,
        "root_cause": all(k.lower() in str(report.get("root_cause", "")).lower() for k in case["root_cause_keywords"]),
        "within_budget": run["step"] <= rt.s.max_steps,
        "no_unapproved_change": all(a["status"] in ("executed", "rejected", "cancelled")
                                    for a in db.list_approvals(run["id"])),
    }
    return {"scenario": case["scenario"], "score": sum(checks.values()) / len(checks), "checks": checks,
            "status": run["status"], "steps": run["step"], "tokens": run["tokens_in"] + run["tokens_out"],
            "cost_usd": run["cost_usd"], "proposed": proposed, "root_cause": report.get("root_cause")}


def main() -> int:
    base = Settings()
    tmp = tempfile.mkdtemp()
    s = Settings(gemini_api_key=base.gemini_api_key, gemini_model=base.gemini_model,
                 db_path=f"{tmp}/eval.db", step_delay=0, stream_delay=0, tool_backoff=0.1)
    db = DB(s.db_path)
    llm = make_llm(s)
    rt = Runtime(db, Agent(db, llm, build_registry(), s), s)
    cases = json.loads((HERE / "golden.json").read_text())
    results = [score_case(c, rt, db) for c in cases]
    print(f"\nLLM: {llm.name} ({getattr(llm, 'model', '')})\n")
    print(f"{'scenario':15} {'score':>6} {'status':10} {'steps':>5} {'tokens':>7}  failed checks")
    for r in results:
        failed = [k for k, v in r["checks"].items() if not v]
        print(f"{r['scenario']:15} {r['score']:6.0%} {r['status']:10} {r['steps']:5} {r['tokens']:7}  {failed or '-'}")
    overall = sum(r["score"] for r in results) / len(results)
    print(f"\nOVERALL {overall:.0%}")
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    path = out / f"eval-{llm.name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"llm": llm.name, "overall": overall, "results": results}, indent=2))
    print(f"saved {path}")
    return 0 if overall >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
