"""The happy path and human-in-the-loop behaviour, with the deterministic mock LLM."""
from tests.conftest import make_runtime


def types(db, rid):
    return [e["type"] for e in db.events(rid)]


def test_full_run_plan_tools_approval_verify_report(db_path):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "waiting_approval"
    appr = db.pending_approval(run["id"])
    assert appr["tool"] == "rollback_deployment" and appr["args"] == {"service": "payments-api", "to_version": "v2.3.9"}
    # nothing changed in "production" while waiting for the human
    assert run["state"]["world"]["actions_applied"] == []
    rt.decide(run["id"], True, "go")
    run = rt.wait(run["id"])
    assert run["status"] == "completed"
    assert run["report"]["confidence"] >= 0.6 and "v2.4.0" in run["report"]["root_cause"]
    assert run["state"]["world"]["services"]["payments-api"]["status"] == "healthy"
    t = types(db, run["id"])
    for expected in ["plan", "thought", "tool_call", "tool_result", "retry", "approval_requested",
                     "approval_decided", "run_finished", "memory_saved", "llm_call"]:
        assert expected in t, expected
    # read-only tools were requested together in one step (parallel execution)
    assert any(len(s["calls"]) == 3 for s in run["state"]["steps"])
    assert run["tokens_in"] > 0 and run["cost_usd"] > 0


def test_reject_means_no_change_and_agent_escalates(db_path):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run("memory-leak")["id"])
    rt.decide(run["id"], False, "not during peak hours")
    run = rt.wait(run["id"])
    assert run["status"] == "escalated" and run["report"]["routed_to_human"]
    assert run["state"]["world"]["actions_applied"] == []
    assert any("not during peak hours" in n for n in run["state"]["notes"])


def test_low_confidence_routes_to_human(db_path):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run("ghost-alert")["id"])
    assert run["status"] == "escalated" and run["report"]["confidence"] < 0.6
    assert db.list_approvals(run["id"]) == []  # did not invent a fix
    assert db.list_memory() == []              # unconfirmed runs are not promoted to long-term memory


def test_prompt_injection_in_logs_is_flagged_and_cannot_bypass_gate(db_path):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run("traffic-spike")["id"])
    warnings = [e for e in db.events(run["id"]) if e["type"] == "warning"]
    assert any("prompt injection" in w["data"]["message"] for w in warnings)
    assert [a["tool"] for a in db.list_approvals(run["id"])] == ["scale_service"]  # no rollbacks
    rt.decide(run["id"], True)
    assert rt.wait(run["id"])["status"] == "completed"


def test_long_term_memory_is_recalled_in_next_run(db_path):
    rt, db = make_runtime(db_path)
    r1 = rt.wait(rt.start_run("bad-deploy")["id"]); rt.decide(r1["id"], True); rt.wait(r1["id"])
    assert db.list_memory()[0]["service"] == "payments-api"
    r2 = rt.wait(rt.start_run("bad-deploy")["id"])
    llm_calls = [e for e in db.events(r2["id"]) if e["type"] == "llm_call"]
    assert any(e["data"]["memories_used"] >= 1 for e in llm_calls)


def test_guidance_reaches_the_agent(db_path):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    rt.add_guidance(run["id"], "Prefer rollback over restart")
    rt.decide(run["id"], True)
    run = rt.wait(run["id"])
    thoughts = [e["data"]["text"] for e in db.events(run["id"]) if e["type"] == "thought"]
    assert any("Prefer rollback" in t for t in thoughts)
