"""Sources / done / not done are derived from the checkpointed run state only."""
from incident_triage_agent.agent.sources import summarize
from tests.agent.test_plan_answer import finished_run


def test_completed_run_lists_sources_and_the_executed_action(db_path):
    _, _, run = finished_run(db_path)
    out = summarize(run["state"], run["report"])
    assert {s["source"] for s in out["sources"]} >= {"Service list", "Metrics", "Logs", "Deployments", "Runbooks"}
    assert all(isinstance(s["step"], int) for s in out["sources"])
    assert len(out["done"]) == 1 and "v2.3.9" in out["done"][0]["text"] and out["not_done"] == []


def test_rejected_action_and_escalation_are_not_done(db_path):
    _, _, run = finished_run(db_path, approve=False)
    out = summarize(run["state"], run["report"])
    assert out["done"] == []
    assert any("rejected" in n["text"] for n in out["not_done"])
    assert any("handed to a human" in n["text"] for n in out["not_done"])


def test_nothing_wrong_means_no_change_proposed(db_path):
    _, _, run = finished_run(db_path, scenario="ghost-alert")
    texts = [n["text"] for n in summarize(run["state"], run["report"])["not_done"]]
    assert texts[0] == "No production change was proposed." and "handed to a human" in texts[1]
