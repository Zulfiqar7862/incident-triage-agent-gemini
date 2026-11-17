from app.sim.world import build_world
from app.tools.infra_tools import build_registry
from app.tools.registry import execute_with_retry

reg = build_registry()


def test_schemas_are_structured_and_risky_flags_set():
    names = {t["name"]: t for t in reg.schemas()}
    assert {"restart_service", "rollback_deployment", "scale_service"} == {n for n, t in names.items() if t["risky"]}
    assert names["get_metrics"]["parameters"]["required"] == ["service"]


def test_transient_error_is_retried_with_backoff():
    world, sleeps = build_world("bad-deploy"), []
    obs = execute_with_retry(reg.get("get_metrics"), world, {"service": "payments-api"}, max_retries=3,
                             backoff=0.1, sleep=sleeps.append)
    assert obs["ok"] and obs["attempts"] == 2 and sleeps == [0.1]


def test_retries_exhausted_returns_error_not_exception():
    world, sleeps = build_world("bad-deploy"), []
    world["flaky"]["get_metrics"] = 10
    obs = execute_with_retry(reg.get("get_metrics"), world, {"service": "payments-api"}, max_retries=3,
                             backoff=0.1, sleep=sleeps.append)
    assert not obs["ok"] and obs["attempts"] == 4 and sleeps == [0.1, 0.2, 0.4]  # exponential


def test_permanent_error_and_validation_are_not_retried():
    world = build_world("bad-deploy")
    obs = execute_with_retry(reg.get("get_logs"), world, {"service": "nope"}, sleep=lambda _: None)
    assert not obs["ok"] and "not found" in obs["error"] and obs["attempts"] == 1
    obs = execute_with_retry(reg.get("scale_service"), world, {"service": "search-api", "replicas": "six"})
    assert not obs["ok"] and "must be integer" in obs["error"]
    obs = execute_with_retry(reg.get("rollback_deployment"), world, {"service": "payments-api", "to_version": "v1"})
    assert not obs["ok"] and "rollback targets" in obs["error"]  # error tells the LLM how to self-correct


def test_runbook_search_returns_top_two_relevant():
    obs = execute_with_retry(reg.get("search_runbooks"), {}, {"query": "memory leak oomkilled restart"})
    assert obs["data"][0]["runbook"] == "memory-leak-oom" and len(obs["data"]) <= 2
