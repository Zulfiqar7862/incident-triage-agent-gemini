"""Plan first, answer last: every run opens with a visible plan and ends with a final answer."""
from incident_triage_agent.agent.answer import build_answer
from incident_triage_agent.llm.mock import MockLLM
from tests.conftest import make_runtime


def finished_run(db_path, scenario="bad-deploy", approve=True):
    rt, db = make_runtime(db_path)
    run = rt.wait(rt.start_run(scenario)["id"])
    if run["status"] == "waiting_approval":
        rt.decide(run["id"], approve, "ok")
        run = rt.wait(run["id"])
    return rt, db, run


def test_plan_comes_before_any_tool_call(db_path):
    _, db, run = finished_run(db_path)
    types = [e["type"] for e in db.events(run["id"])]
    assert types.index("plan") < types.index("tool_call")
    assert run["plan"] and len(run["plan"]) >= 3


def test_model_is_offered_only_update_plan_until_a_plan_exists(db_path):
    offered = []

    class Recording(MockLLM):
        def decide(self, system, prompt, tools, state):
            offered.append([t["name"] for t in tools])
            return super().decide(system, prompt, tools, state)

    rt, _ = make_runtime(db_path, llm=Recording())
    rt.wait(rt.start_run("bad-deploy")["id"])
    assert offered[0] == ["update_plan"]
    assert len(offered[1]) > 1          # all tools once the plan exists


def test_answer_is_the_last_event_and_matches_the_report(db_path):
    _, db, run = finished_run(db_path)
    last = db.events(run["id"])[-1]
    assert last["type"] == "answer"
    assert last["data"] == build_answer("completed", run["report"]).model_dump()
    assert last["data"]["action_taken"] == "payments-api rolled back to v2.3.9"
    assert last["data"]["outcome"].startswith("Fixed and verified")


def test_escalated_answer_says_no_change_and_human_takes_over(db_path):
    _, db, run = finished_run(db_path, approve=False)
    answer = db.events(run["id"])[-1]["data"]
    assert run["status"] == "escalated"
    assert answer["action_taken"] == "No change was made" and answer["outcome"].startswith("Handed to a human")


def test_plan_and_answer_survive_a_restart(db_path):
    rt1, db1 = make_runtime(db_path)
    rid = rt1.wait(rt1.start_run("bad-deploy")["id"])["id"]   # stops at the approval, plan already written
    plan = db1.get_run(rid)["plan"]
    db1.close()                                                 # "kill" the server

    rt2, db2 = make_runtime(db_path)
    rt2.recover()
    assert db2.get_run(rid)["plan"] == plan
    rt2.decide(rid, True, "approved after restart")
    run = rt2.wait(rid)
    assert db2.events(rid)[-1]["type"] == "answer" and run["plan"] == plan
