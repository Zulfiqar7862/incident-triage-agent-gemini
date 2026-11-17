"""Error recovery and guard rails, using a scripted LLM to force edge cases."""
from app.llm.base import LLMDecision, LLMError
from tests.conftest import ScriptedLLM, call, decision, make_runtime


def test_step_limit_and_loop_guard(db_path):
    llm = ScriptedLLM([decision(call("list_services"))])  # a "stuck" model that repeats itself
    rt, db = make_runtime(db_path, llm=llm, max_steps=5)
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "escalated" and "step limit" in run["report"]["root_cause"]
    msgs = [e["data"]["message"] for e in db.events(run["id"]) if e["type"] == "warning"]
    assert any("Loop guard" in m for m in msgs)


def test_token_budget_stops_the_run(db_path):
    rt, db = make_runtime(db_path, llm=ScriptedLLM([decision(call("list_services"))]), max_run_tokens=250)
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "escalated" and "token budget" in run["report"]["root_cause"]


def test_llm_outage_fails_run_and_resume_continues(db_path):
    from app.llm.mock import MockLLM
    mock = MockLLM()
    outage = {"on": True}

    def script(state):
        if outage["on"] and len(state.get("steps", [])) == 2:
            raise LLMError("503 from model")
        return mock.decide("", "", [], state)
    rt, db = make_runtime(db_path, llm=ScriptedLLM(script))
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "failed" and "503" in run["error"]
    outage["on"] = False
    rt.resume(run["id"])
    assert rt.wait(run["id"])["status"] == "waiting_approval"


def test_self_correction_after_bad_arguments(db_path):
    script = [
        decision(call("update_plan", steps=["investigate", "fix"])),
        decision(call("get_logs", service="payments")),          # wrong name -> error observation
        decision(call("get_logs", service="payments-api")),      # corrected
        decision(call("finish_triage", root_cause="bad deploy", severity="SEV2", confidence=0.8)),
    ]
    llm = ScriptedLLM(script)
    rt, db = make_runtime(db_path, llm=llm)
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    calls = [c for s in run["state"]["steps"] for c in s["calls"]]
    assert calls[1]["ok"] is False and "Known services" in calls[1]["error"]
    assert calls[2]["ok"] is True and run["status"] == "completed"


def test_unknown_tool_and_no_tool_reply_are_handled(db_path):
    script = [LLMDecision(thought="just text", calls=[]), decision(call("delete_cluster")),
              decision(call("finish_triage", root_cause="x", severity="SEV4", confidence=0.1))]
    rt, db = make_runtime(db_path, llm=ScriptedLLM(script))
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "escalated"
    assert any("without a tool call" in n for n in run["state"]["notes"])
    assert "unknown tool" in run["state"]["steps"][1]["calls"][0]["error"]


def test_only_one_risky_action_per_turn(db_path):
    script = [decision(call("restart_service", service="payments-api"), call("scale_service", service="search-api", replicas=4))]
    rt, db = make_runtime(db_path, llm=ScriptedLLM(script))
    run = rt.wait(rt.start_run("bad-deploy")["id"])
    assert run["status"] == "waiting_approval" and len(db.list_approvals(run["id"])) == 1
