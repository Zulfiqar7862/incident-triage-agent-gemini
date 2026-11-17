"""Durable execution: state survives a process restart (simulated by a brand-new Runtime + DB connection)."""
from app.agent.loop import CONTINUE
from tests.conftest import make_runtime


def test_restart_while_waiting_for_approval(db_path):
    rt1, db1 = make_runtime(db_path)
    rid = rt1.wait(rt1.start_run("bad-deploy")["id"])["id"]
    db1.close()                                   # "kill" the server

    rt2, db2 = make_runtime(db_path)              # "restart" the server
    assert rt2.recover() == []                    # waiting runs don't need a worker
    run = db2.get_run(rid)
    assert run["status"] == "waiting_approval" and db2.pending_approval(rid)
    rt2.decide(rid, True, "approved after restart")
    run = rt2.wait(rid)
    assert run["status"] == "completed"
    assert len(run["state"]["world"]["actions_applied"]) == 1


def test_crash_mid_run_resumes_from_last_checkpoint(db_path):
    rt1, db1 = make_runtime(db_path)
    run = db1.create_run("bad-deploy", "t", "payments-api errors", "mock",
                         {"world": __import__("app.sim.world", fromlist=["x"]).build_world("bad-deploy"),
                          "steps": [], "notes": [], "pending": None})
    db1.set_status(run["id"], "running")
    assert rt1.agent.step(run["id"]) == CONTINUE   # plan
    assert rt1.agent.step(run["id"]) == CONTINUE   # list_services
    steps_before = db1.get_run(run["id"])["step"]
    db1.close()                                    # crash: no worker is driving this 'running' run

    rt2, db2 = make_runtime(db_path)
    assert rt2.recover() == [run["id"]]
    resumed = rt2.wait(run["id"])
    assert resumed["status"] == "waiting_approval"
    assert resumed["step"] > steps_before
    assert resumed["state"]["steps"][0]["calls"][0]["tool"] == "update_plan"   # history kept, not redone
    assert "resumed_after_restart" in [e["type"] for e in db2.events(run["id"])]


def test_approved_action_runs_exactly_once_across_a_crash(db_path):
    rt1, db1 = make_runtime(db_path)
    rid = rt1.wait(rt1.start_run("traffic-spike")["id"])["id"]
    appr = db1.pending_approval(rid)
    # Operator approved, then the server died before the worker executed the action
    db1.decide_approval(appr["id"], "approved", "")
    db1.set_status(rid, "running", ("waiting_approval",))
    db1.close()

    rt2, db2 = make_runtime(db_path)
    rt2.recover()
    run = rt2.wait(rid)
    assert run["status"] == "completed"
    assert [a["action"] for a in run["state"]["world"]["actions_applied"]] == ["scale_service"]
    assert db2.get_approval(appr["id"])["status"] == "executed"


def test_pause_and_resume(db_path):
    rt, db = make_runtime(db_path)
    rid = db.create_run("bad-deploy", "t", "a", "mock", {"world": __import__("app.sim.world", fromlist=["x"]).build_world("bad-deploy"),
                                                       "steps": [], "notes": [], "pending": None})["id"]
    db.set_status(rid, "paused")
    rt.resume(rid)
    assert rt.wait(rid)["status"] == "waiting_approval"
    rt.cancel(rid)
    assert db.get_run(rid)["status"] == "cancelled"
    assert db.list_approvals(rid)[0]["status"] == "cancelled"
