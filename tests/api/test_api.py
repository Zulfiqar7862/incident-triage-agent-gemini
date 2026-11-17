"""HTTP API + UI, through FastAPI's TestClient (mock LLM)."""
import time

from fastapi.testclient import TestClient

from incident_triage_agent.api.main import create_app
from incident_triage_agent.llm.mock import MockLLM
from tests.conftest import make_settings


def client(db_path, **kw):
    return TestClient(create_app(make_settings(db_path, **kw), llm=MockLLM()))


def wait_status(c, rid, not_in=("running", "pending"), timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] not in not_in:
            return r
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_meta_endpoints_and_ui(db_path):
    with client(db_path) as c:
        assert c.get("/health").json()["llm_mode"] == "mock"
        assert len(c.get("/api/scenarios").json()) == 4
        assert any(t["risky"] for t in c.get("/api/tools").json())
        page = c.get("/")
        assert page.status_code == 200 and "Incident Triage Agent" in page.text
        assert c.get("/static/app.js").status_code == 200


def test_full_run_over_http(db_path):
    with client(db_path) as c:
        r = c.post("/api/runs", json={"scenario": "bad-deploy"})
        assert r.status_code == 201
        rid = r.json()["id"]
        run = wait_status(c, rid)
        assert run["status"] == "waiting_approval" and run["pending_approval"]["tool"] == "rollback_deployment"
        assert c.post(f"/api/runs/{rid}/pause").status_code == 409          # can't pause while waiting
        assert c.post(f"/api/runs/{rid}/guidance", json={"message": "go ahead"}).status_code == 200
        ap = c.post(f"/api/runs/{rid}/approval", json={"decision": "approve", "comment": "ok"})
        assert ap.status_code == 200 and ap.json()["status"] == "approved"
        run = wait_status(c, rid, not_in=("running", "pending", "waiting_approval"))
        assert run["status"] == "completed" and run["infra"]["payments-api"]["status"] == "healthy"
        assert c.post(f"/api/runs/{rid}/approval", json={"decision": "approve"}).status_code == 409
        evs = c.get(f"/api/runs/{rid}/events").json()
        assert evs and c.get(f"/api/runs/{rid}/events?after={evs[-1]['id']}").json() == []
        assert c.get("/api/memory").json()[0]["service"] == "payments-api"
        assert any(x["id"] == rid for x in c.get("/api/runs").json())
        with c.stream("GET", f"/api/runs/{rid}/stream") as s:   # SSE replays the trace then ends
            body = "".join(s.iter_text())
        assert "event: trace" in body and "event: end" in body


def test_pause_resume_cancel_over_http(db_path):
    with client(db_path, step_delay=0.3) as c:
        rid = c.post("/api/runs", json={"scenario": "memory-leak"}).json()["id"]
        assert c.post(f"/api/runs/{rid}/pause").json()["status"] == "paused"
        time.sleep(0.5)
        paused_step = c.get(f"/api/runs/{rid}").json()["step"]
        time.sleep(0.6)
        assert c.get(f"/api/runs/{rid}").json()["step"] == paused_step     # really stopped
        assert c.post(f"/api/runs/{rid}/resume").json()["status"] == "running"
        assert c.post(f"/api/runs/{rid}/cancel").json()["status"] == "cancelled"


def test_errors(db_path):
    with client(db_path) as c:
        assert c.post("/api/runs", json={"scenario": "nope"}).status_code == 400
        assert c.get("/api/runs/missing").status_code == 404
        assert c.post("/api/runs/missing/approval", json={"decision": "approve"}).status_code == 404
        assert c.post("/api/runs/x/approval", json={"decision": "maybe"}).status_code == 422
