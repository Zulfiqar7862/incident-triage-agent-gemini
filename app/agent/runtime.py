"""Runtime: lifecycle of runs (start, pause, resume, cancel, approve) and the worker threads.

Each active run is driven by one background thread that calls `agent.step()`
until the run is no longer `running`. Control actions only change the status in
SQLite (conditional updates); the worker notices at the next step boundary.
On startup `recover()` restarts workers for runs that were `running` when the
process died - that is durable execution in ~20 lines.
"""
from __future__ import annotations

import threading
import time

from app.agent.loop import STOP, Agent
from app.config import Settings
from app.db import DB, TERMINAL
from app.sim.world import SCENARIOS, build_world


class ConflictError(Exception):
    """The requested action is not valid in the run's current state (HTTP 409)."""


class Runtime:
    def __init__(self, db: DB, agent: Agent, settings: Settings):
        self.db, self.agent, self.s = db, agent, settings
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}

    # ---------------------------------------------------------------- lifecycle
    def start_run(self, scenario: str, alert: str | None = None) -> dict:
        world = build_world(scenario)  # raises ValueError for unknown scenario
        meta = SCENARIOS[scenario]
        run = self.db.create_run(scenario, meta["title"], alert or meta["alert"], self.agent.llm.name,
                                 {"world": world, "steps": [], "notes": [], "pending": None})
        self.db.add_event(run["id"], 0, "run_created", {"scenario": scenario, "alert": run["alert"],
                                                         "llm": self.agent.llm.name})
        self.db.set_status(run["id"], "running", ("pending",))
        self._spawn(run["id"])
        return self.db.get_run(run["id"])  # type: ignore[return-value]

    def pause(self, run_id: str) -> None:
        self._transition(run_id, "paused", ("running",), "paused by operator")

    def resume(self, run_id: str) -> None:
        # failed runs (e.g. LLM outage) can be resumed from their last checkpoint too
        self._transition(run_id, "running", ("paused", "failed"), "resumed by operator")
        self._spawn(run_id)

    def cancel(self, run_id: str) -> None:
        run = self._get(run_id)
        self._transition(run_id, "cancelled", ("pending", "running", "paused", "waiting_approval", "failed"),
                         "cancelled by operator")
        appr = self.db.pending_approval(run["id"])
        if appr:
            self.db.decide_approval(appr["id"], "cancelled", "run cancelled")

    def decide(self, run_id: str, approve: bool, comment: str = "") -> dict:
        run = self._get(run_id)
        appr = self.db.pending_approval(run_id)
        if run["status"] != "waiting_approval" or appr is None:
            raise ConflictError(f"run is '{run['status']}', no approval pending")
        decision = "approved" if approve else "rejected"
        if not self.db.decide_approval(appr["id"], decision, comment):
            raise ConflictError("approval was already decided")
        self.db.add_event(run_id, run["step"], "approval_decided",
                          {"approval_id": appr["id"], "decision": decision, "comment": comment,
                           "tool": appr["tool"], "args": appr["args"]})
        self._transition(run_id, "running", ("waiting_approval",), f"approval {decision}")
        self._spawn(run_id)
        return self.db.get_approval(appr["id"])  # type: ignore[return-value]

    def add_guidance(self, run_id: str, message: str) -> None:
        run = self._get(run_id)
        if run["status"] in TERMINAL:
            raise ConflictError(f"run is already {run['status']}")
        self.db.add_event(run_id, run["step"], "guidance", {"message": message[:500]})

    def recover(self) -> list[str]:
        """Called on startup: continue every run that was in flight when the process stopped."""
        ids = self.db.runs_with_status(("pending", "running"))
        for rid in ids:
            self.db.set_status(rid, "running", ("pending", "running"))
            self.db.add_event(rid, self._get(rid)["step"], "resumed_after_restart",
                              {"message": "server restarted; continuing from last checkpoint"})
            self._spawn(rid)
        return ids

    # ---------------------------------------------------------------- internals
    def _get(self, run_id: str) -> dict:
        run = self.db.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _transition(self, run_id: str, new: str, expected: tuple, why: str) -> None:
        run = self._get(run_id)
        if not self.db.set_status(run_id, new, expected):
            raise ConflictError(f"cannot go from '{run['status']}' to '{new}'")
        self.db.add_event(run_id, run["step"], "status", {"from": run["status"], "to": new, "why": why})

    def _spawn(self, run_id: str) -> None:
        with self._lock:
            t = self._workers.get(run_id)
            if t and t.is_alive():
                return  # the existing worker will see the new status at its next check
            t = threading.Thread(target=self._drive, args=(run_id,), name=f"run-{run_id}", daemon=True)
            self._workers[run_id] = t
            t.start()

    def _drive(self, run_id: str) -> None:
        while True:
            with self._lock:  # status check + deregistration are atomic w.r.t. _spawn
                run = self.db.get_run(run_id)
                if run is None or run["status"] != "running":
                    self._workers.pop(run_id, None)
                    return
            try:
                result = self.agent.step(run_id)
            except Exception as e:  # a bug must not kill silently: fail the run, keep the checkpoint
                self.db.add_event(run_id, run["step"], "error", {"message": f"{type(e).__name__}: {e}"})
                self.db.set_status(run_id, "failed", ("running",), error=f"{type(e).__name__}: {e}")
                continue
            if result != STOP and self.s.step_delay:
                time.sleep(self.s.step_delay)

    def wait(self, run_id: str, timeout: float = 10.0) -> dict:
        """Block until the run's worker is idle (used by tests and scripts)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                t = self._workers.get(run_id)
            if t is None or not t.is_alive():
                return self._get(run_id)
            t.join(0.05)
        return self._get(run_id)
