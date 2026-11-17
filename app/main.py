"""FastAPI app: REST API for the agent runtime + the operator UI (static/index.html).

Run:  uvicorn app.main:app --reload   (or `make run`)
Docs: http://localhost:8000/docs (auto-generated OpenAPI)
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.loop import Agent
from app.agent.runtime import ConflictError, Runtime
from app.config import Settings
from app.db import DB, TERMINAL
from app.llm import LLM, make_llm
from app.sim.world import SCENARIOS
from app.tools.infra_tools import build_registry

STATIC = Path(__file__).resolve().parents[1] / "static"


class StartRun(BaseModel):
    scenario: str = Field(examples=["bad-deploy"])
    alert: str | None = Field(default=None, description="Optional custom alert text")


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class Guidance(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or Settings()
    db = DB(settings.db_path)
    registry = build_registry()
    llm = llm or make_llm(settings)
    runtime = Runtime(db, Agent(db, llm, registry, settings), settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resumed = runtime.recover()  # durable execution: continue runs interrupted by a restart
        if resumed:
            print(f"[startup] resumed {len(resumed)} interrupted run(s): {resumed}")
        yield

    app = FastAPI(title="Incident Triage Agent", version="1.0", lifespan=lifespan)
    app.state.runtime, app.state.db, app.state.settings = runtime, db, settings

    def get_run_or_404(run_id: str) -> dict:
        run = db.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        return run

    def guarded(fn, *args):
        try:
            return fn(*args)
        except ConflictError as e:
            raise HTTPException(409, str(e))
        except KeyError:
            raise HTTPException(404, "run not found")

    def run_view(run: dict) -> dict:
        state = run.pop("state")
        world = state.get("world", {})
        run["pending_approval"] = db.pending_approval(run["id"])
        run["approvals"] = db.list_approvals(run["id"])
        run["steps"] = state.get("steps", [])
        run["notes"] = state.get("notes", [])
        run["infra"] = {n: {"status": s["status"], "version": s["version"], "replicas": s["replicas"]}
                        for n, s in world.get("services", {}).items()}
        return run

    # ------------------------------------------------------------ meta
    @app.get("/health")
    def health():
        return {"status": "ok", "llm_mode": settings.llm_mode,
                "model": settings.gemini_model if settings.llm_mode == "gemini" else "mock-policy-v1"}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"id": k, **v} for k, v in SCENARIOS.items()]

    @app.get("/api/tools")
    def tools():
        return registry.schemas()

    @app.get("/api/memory")
    def memory():
        return db.list_memory()

    # ------------------------------------------------------------ runs
    @app.post("/api/runs", status_code=201)
    def start_run(body: StartRun):
        try:
            run = runtime.start_run(body.scenario, body.alert)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return run_view(run)

    @app.get("/api/runs")
    def list_runs():
        return db.list_runs()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return run_view(get_run_or_404(run_id))

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0):
        get_run_or_404(run_id)
        return db.events(run_id, after)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, after: int = 0):
        """Server-Sent Events: pushes new trace events as they are written."""
        get_run_or_404(run_id)

        async def gen():
            last, idle = after, 0
            while True:
                evs = db.events(run_id, last)
                for e in evs:
                    last = e["id"]
                    yield f"id: {e['id']}\nevent: trace\ndata: {json.dumps(e, default=str)}\n\n"
                run = db.get_run(run_id)
                if run and run["status"] in TERMINAL and not evs:
                    idle += 1
                    if idle > 3:
                        yield "event: end\ndata: {}\n\n"
                        return
                yield ": keep-alive\n\n" if not evs else ""
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str):
        guarded(runtime.pause, run_id)
        return run_view(get_run_or_404(run_id))

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        guarded(runtime.resume, run_id)
        return run_view(get_run_or_404(run_id))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        guarded(runtime.cancel, run_id)
        return run_view(get_run_or_404(run_id))

    @app.post("/api/runs/{run_id}/approval")
    def approval(run_id: str, body: Decision):
        get_run_or_404(run_id)
        return guarded(runtime.decide, run_id, body.decision == "approve", body.comment)

    @app.post("/api/runs/{run_id}/guidance")
    def guidance(run_id: str, body: Guidance):
        get_run_or_404(run_id)
        guarded(runtime.add_guidance, run_id, body.message)
        return {"ok": True}

    # ------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
