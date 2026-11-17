"""The agent: model + tools + guards, one step at a time.

`Agent.step(run_id)` does exactly ONE step and checkpoints it:

  0. a decided approval?  -> run or skip the gated action      (approval.py, executor.py)
  1. guards               -> step limit, token budget           (guards.py)
  2. plan                 -> build context, ask the model       (planner.py)
  3. dispatch             -> control tools, read-only tools in
                             parallel, risky tool -> approval   (executor.py, guards.py, approval.py)
  4. checkpoint           -> save the run state to SQLite       (state.py)

Every step starts from the database and ends with a checkpoint, so the process
can die at any moment and the run continues from the last checkpoint.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from incident_triage_agent.agent import guards
from incident_triage_agent.agent.approval import ApprovalGate, route
from incident_triage_agent.agent.executor import Executor
from incident_triage_agent.agent.planner import Planner
from incident_triage_agent.agent.safety import redact
from incident_triage_agent.agent.state import Checkpointer, RunState, cost_usd
from incident_triage_agent.config import Settings
from incident_triage_agent.db import DB
from incident_triage_agent.llm.base import LLM, LLMDecision, LLMError, ToolCall
from incident_triage_agent.tools.registry import ToolRegistry

CONTINUE, STOP = "continue", "stop"
MAX_PLAN_ITEMS = 10


class Agent:
    def __init__(self, db: DB, llm: LLM, registry: ToolRegistry, settings: Settings,
                 sleep: Callable[[float], None] | None = None):
        self.db, self.llm, self.registry, self.s = db, llm, registry, settings
        self.planner = Planner(db, llm, registry, settings)
        self.executor = Executor(registry, settings, sleep)
        self.gate = ApprovalGate(db)
        self.checkpoints = Checkpointer(db, settings)

    # ------------------------------------------------------------------ helpers
    def _event(self, run_id: str, step: int, type_: str, **data) -> None:
        self.db.add_event(run_id, step, type_, data)

    def _guidance(self, run_id: str) -> list[str]:
        return [e["data"]["message"] for e in self.db.events(run_id) if e["type"] == "guidance"]

    def _tool_result_event(self, rs: RunState, rec: dict, **extra) -> None:
        self._event(rs.run_id, rs.step, "tool_result", tool=rec["tool"], args=rec["args"], ok=rec["ok"],
                    summary=rec["summary"], error=rec.get("error"), attempts=rec["attempts"],
                    latency_ms=rec["latency_ms"], **extra)

    def _finish(self, rs: RunState, args: dict) -> str:
        confidence = float(args.get("confidence", 0))
        report = {k: args.get(k) for k in ("root_cause", "severity", "affected_services", "actions_taken",
                                           "follow_ups")}
        report["confidence"] = confidence
        report["actions_applied"] = rs.state["world"].get("actions_applied", [])
        status, reason = route(confidence, bool(report["actions_applied"]), self.s.min_confidence)
        report["routed_to_human"] = bool(reason)
        report["routing_reason"] = reason or "mitigated and verified with high confidence"
        self.checkpoints.save(rs)
        if not self.db.set_status(rs.run_id, status, ("running",), report=report):
            return STOP
        self._event(rs.run_id, rs.step, "run_finished", status=status, report=report)
        # memory promotion: only confident, mitigated incidents become long-term memory (redacted)
        if status == "completed":
            for svc in report.get("affected_services") or []:
                self.db.add_memory(svc, redact(rs.run["alert"])[:300], redact(str(report["root_cause"]))[:400],
                                   redact("; ".join(report.get("actions_taken") or ["none"]))[:300], rs.run_id)
            self._event(rs.run_id, rs.step, "memory_saved", services=report.get("affected_services") or [])
        return STOP

    # ------------------------------------------------------------------ 0) approvals
    def _resume_gated_action(self, rs: RunState) -> str:
        status, appr = self.gate.decision(rs)
        if status == "pending":
            self.db.set_status(rs.run_id, "waiting_approval", ("running",))
            return STOP
        executed = None
        if status == "approved":
            pending = rs.state["pending"]
            rec = self.executor.run(rs.state["world"], ToolCall(pending["tool"], pending["args"]))
            rec["approval"] = {"id": appr["id"], "status": "approved", "comment": appr.get("comment")}
            executed = appr["id"]
            self._tool_result_event(rs, rec, risky=True)
        else:  # rejected / cancelled
            rec = self.gate.rejected_record(rs, status, appr)
        self.gate.close(rs, rec)
        # the action's result and "approval executed" are written together: exactly once across crashes
        self.checkpoints.save(rs, executed_approval=executed)
        return CONTINUE

    # ------------------------------------------------------------------ the step
    def step(self, run_id: str) -> str:
        run = self.db.get_run(run_id)
        if run is None or run["status"] != "running":
            return STOP
        rs = RunState.load(run)

        # 0) resume a gated action once the human has decided
        if rs.state.get("pending"):
            return self._resume_gated_action(rs)

        # 1) guards
        stop_report = guards.limit_reached(rs, self.s)
        if stop_report:
            return self._finish(rs, stop_report)

        # 2) plan: build context and ask the model
        try:
            decision, layers, memories = self.planner.next_decision(rs, self._guidance(run_id))
        except LLMError as e:
            self._event(run_id, rs.step, "error", message=str(e))
            self.db.set_status(run_id, "failed", ("running",), error=str(e))
            return STOP
        step_rec = self._record_decision(rs, decision, layers, memories)
        if not decision.calls:
            rs.state.setdefault("notes", []).append(f"Step {rs.step}: you replied without a tool call. "
                                                    "Always call a tool or finish_triage.")
            self.checkpoints.save(rs)
            return CONTINUE

        # 3) dispatch
        reads, risky = [], []
        for call in decision.calls:
            if call.name == "finish_triage":
                step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": True,
                                          "summary": "finished", "attempts": 1, "latency_ms": 0})
                return self._finish(rs, call.args)
            kind = self._classify(rs, step_rec, call)
            if kind == "read":
                reads.append(call)
            elif kind == "risky":
                risky.append(call)
        self._run_reads(rs, step_rec, reads)
        if risky:
            return self._request_approval(rs, step_rec, risky, decision.thought)

        # 4) checkpoint
        self.checkpoints.save(rs)
        return CONTINUE

    # ------------------------------------------------------------------ step parts
    def _record_decision(self, rs: RunState, decision: LLMDecision, layers: dict, memories: list) -> dict:
        """Count the model call, emit llm_call + thought events, and open a new step record."""
        rs.step += 1
        n = rs.step
        rs.tokens_in += decision.tokens_in
        rs.tokens_out += decision.tokens_out
        self._event(rs.run_id, n, "llm_call", model=decision.model, tokens_in=decision.tokens_in,
                    tokens_out=decision.tokens_out, latency_ms=decision.latency_ms,
                    cost_usd=round(cost_usd(self.s, decision.tokens_in, decision.tokens_out), 6),
                    context_layers=layers, memories_used=len(memories))
        if not decision.thought:  # some models skip the text part; still tell the operator what is happening
            decision.thought = "(no reasoning text from model) calling " + ", ".join(c.name for c in decision.calls)
        self._event(rs.run_id, n, "thought", text=decision.thought)
        if guards.near_token_budget(rs, self.s):
            self._event(rs.run_id, n, "warning", message="Over 80% of the run token budget used.")
        step_rec: dict = {"n": n, "thought": decision.thought, "calls": []}
        rs.state.setdefault("steps", []).append(step_rec)
        return step_rec

    def _classify(self, rs: RunState, step_rec: dict, call: ToolCall) -> str:
        """Handle control tools and refusals inline; return 'read', 'risky' or 'done'."""
        tool = self.registry.get(call.name)
        if call.name == "update_plan":
            kind = "plan_revised" if rs.plan else "plan"
            rs.plan = [str(x) for x in (call.args.get("steps") or [])][:MAX_PLAN_ITEMS]
            step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": True,
                                      "summary": f"plan with {len(rs.plan)} steps", "attempts": 1, "latency_ms": 0})
            self._event(rs.run_id, rs.step, kind, steps=rs.plan)
            return "done"
        if tool is None:
            step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": False, "attempts": 0,
                                      "latency_ms": 0, "error": f"unknown tool '{call.name}'. "
                                      f"Available: {sorted(self.registry.tools)}",
                                      "summary": f"unknown tool {call.name}"})
            self._event(rs.run_id, rs.step, "warning", message=f"Model called unknown tool {call.name}")
            return "done"
        if tool.risky:
            return "risky"
        if guards.is_repeated(rs.state["steps"], call):
            step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": False, "attempts": 0,
                                      "latency_ms": 0, "summary": "blocked repeated call",
                                      "error": "loop guard: identical call already made twice; "
                                               "use the earlier result or try something else"})
            self._event(rs.run_id, rs.step, "warning", message=f"Loop guard blocked repeated {call.name}")
            return "done"
        return "read"

    def _run_reads(self, rs: RunState, step_rec: dict, reads: list[ToolCall]) -> None:
        """Run read-only calls in parallel and record each result with its events."""
        n = rs.step
        for call in reads:
            self._event(rs.run_id, n, "tool_call", tool=call.name, args=call.args, risky=False)
        for rec in self.executor.run_parallel(rs.state["world"], reads):
            step_rec["calls"].append(rec)
            self._tool_result_event(rs, rec, risky=False,
                                    output=json.dumps(rec.get("data"), default=str)[:2000] if rec["ok"] else None)
            if rec.get("attempts", 1) > 1:
                self._event(rs.run_id, n, "retry", tool=rec["tool"], attempts=rec["attempts"], ok=rec["ok"])
            if rec.get("flagged"):
                self._event(rs.run_id, n, "warning", message=f"Possible prompt injection in {rec['tool']} output. "
                            "It is passed to the model as untrusted data and cannot bypass the approval gate.")

    def _request_approval(self, rs: RunState, step_rec: dict, risky: list[ToolCall], thought: str) -> str:
        """Human-in-the-loop gate (enforced in code, not prompt): one risky action per turn, then wait."""
        for extra in risky[1:]:
            step_rec["calls"].append({"tool": extra.name, "args": extra.args, "ok": False, "attempts": 0,
                                      "latency_ms": 0, "risky": True, "summary": "not run",
                                      "error": "only one risky action per turn; propose it again later if needed"})
        call = risky[0]
        appr = self.gate.request(rs, call, thought or "")
        step_rec["calls"].append({"tool": call.name, "args": call.args, "risky": True, "ok": None,
                                  "awaiting_approval": True, "summary": "waiting for human approval"})
        self.checkpoints.save(rs)
        self._event(rs.run_id, rs.step, "approval_requested", approval_id=appr["id"], tool=call.name, args=call.args,
                    reason=thought)
        self.db.set_status(rs.run_id, "waiting_approval", ("running",))
        return STOP
