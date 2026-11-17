"""The agent loop. `Agent.step(run_id)` does exactly ONE step and checkpoints it.

One step =
  0. if an approval was decided: execute (or skip) the gated action, checkpoint, done
  1. guards: step limit, token budget
  2. build context from durable state (context.py)
  3. ask the LLM what to do next (plan / tools / finish)
  4. dispatch: control tools, read-only tools in parallel, risky tool -> approval gate
  5. checkpoint everything to SQLite

Because every step starts from the database and ends with a checkpoint, the
process can die at any moment and the run continues from the last checkpoint.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.agent.context import build_context
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.safety import looks_like_injection, redact
from app.config import Settings
from app.db import DB
from app.llm.base import LLM, LLMError, ToolCall
from app.tools.registry import ToolRegistry, execute_with_retry

CONTINUE, STOP = "continue", "stop"


class Agent:
    def __init__(self, db: DB, llm: LLM, registry: ToolRegistry, settings: Settings,
                 sleep: Callable[[float], None] | None = None):
        self.db, self.llm, self.registry, self.s = db, llm, registry, settings
        self.sleep = sleep or time.sleep

    # ------------------------------------------------------------------ helpers
    def _event(self, run_id: str, step: int, type_: str, **data) -> None:
        self.db.add_event(run_id, step, type_, data)

    def _guidance(self, run_id: str) -> list[str]:
        return [e["data"]["message"] for e in self.db.events(run_id) if e["type"] == "guidance"]

    def _checkpoint(self, run: dict, state: dict, plan: list, step: int, t_in: int, t_out: int,
                    executed_approval: str | None = None) -> None:
        cost = t_in / 1e6 * self.s.price_input_per_m + t_out / 1e6 * self.s.price_output_per_m
        self.db.save_checkpoint(run["id"], state=state, plan=plan, step=step, tokens_in=t_in, tokens_out=t_out,
                                cost_usd=round(cost, 6), executed_approval=executed_approval)

    def _run_tool(self, world: dict, call: ToolCall) -> dict:
        tool = self.registry.get(call.name)
        assert tool is not None
        obs = execute_with_retry(tool, world, call.args, max_retries=self.s.tool_max_retries,
                                 backoff=self.s.tool_backoff, sleep=self.sleep)
        rec = {"tool": call.name, "args": call.args, "risky": tool.risky, **obs}
        if obs.get("ok") and looks_like_injection(obs.get("data")):
            rec["flagged"] = True
        return rec

    def _finish(self, run: dict, state: dict, plan: list, step: int, t_in: int, t_out: int, args: dict) -> str:
        confidence = float(args.get("confidence", 0))
        report = {k: args.get(k) for k in ("root_cause", "severity", "affected_services", "actions_taken",
                                           "follow_ups")}
        report["confidence"] = confidence
        report["actions_applied"] = state["world"].get("actions_applied", [])
        # Routing rule (in code, not left to the model):
        #   completed = a fix was applied (human-approved) AND the model is confident
        #   escalated = low confidence, or nothing was mitigated -> a human must look
        mitigated = bool(report["actions_applied"])
        if confidence < self.s.min_confidence:
            reason = f"confidence {confidence:.2f} below {self.s.min_confidence}"
        elif not mitigated:
            reason = "no mitigation was applied; a human should confirm and close"
        else:
            reason = ""
        status = "escalated" if reason else "completed"
        report["routed_to_human"] = bool(reason)
        report["routing_reason"] = reason or "mitigated and verified with high confidence"
        self._checkpoint(run, state, plan, step, t_in, t_out)
        if not self.db.set_status(run["id"], status, ("running",), report=report):
            return STOP
        self._event(run["id"], step, "run_finished", status=status, report=report)
        # memory promotion: only confident, finished incidents become long-term memory (redacted)
        if status == "completed":
            for svc in report.get("affected_services") or []:
                self.db.add_memory(svc, redact(run["alert"])[:300], redact(str(report["root_cause"]))[:400],
                                   redact("; ".join(report.get("actions_taken") or ["none"]))[:300], run["id"])
            self._event(run["id"], step, "memory_saved", services=report.get("affected_services") or [])
        return STOP

    # ------------------------------------------------------------------ the step
    def step(self, run_id: str) -> str:
        run = self.db.get_run(run_id)
        if run is None or run["status"] != "running":
            return STOP
        state, plan, n = run["state"], run["plan"], run["step"]
        t_in, t_out = run["tokens_in"], run["tokens_out"]

        # 0) resume a gated action once the human has decided
        pending = state.get("pending")
        if pending:
            appr = self.db.get_approval(pending["approval_id"])
            status = appr["status"] if appr else "cancelled"
            if status == "pending":
                self.db.set_status(run_id, "waiting_approval", ("running",))
                return STOP
            step_rec = next(s for s in state["steps"] if s["n"] == pending["step"])
            executed = None
            if status == "approved":
                rec = self._run_tool(state["world"], ToolCall(pending["tool"], pending["args"]))
                rec["approval"] = {"id": appr["id"], "status": "approved", "comment": appr.get("comment")}
                executed = appr["id"]
                self._event(run_id, n, "tool_result", tool=rec["tool"], args=rec["args"], ok=rec["ok"],
                            summary=rec["summary"], error=rec.get("error"), attempts=rec["attempts"],
                            latency_ms=rec["latency_ms"], risky=True)
            else:  # rejected / cancelled
                comment = (appr or {}).get("comment") or "no comment"
                rec = {"tool": pending["tool"], "args": pending["args"], "risky": True, "ok": False,
                       "error": f"Operator {status} this action: {comment}",
                       "summary": f"{pending['tool']} {status} by operator", "attempts": 0, "latency_ms": 0,
                       "approval": {"id": pending["approval_id"], "status": status, "comment": comment}}
                state.setdefault("notes", []).append(
                    f"The operator {status} {pending['tool']}({json.dumps(pending['args'])}): {comment}. "
                    "Do not retry the same action; adapt or finish.")
            step_rec["calls"] = [c for c in step_rec["calls"] if not c.get("awaiting_approval")] + [rec]
            state["pending"] = None
            self._checkpoint(run, state, plan, n, t_in, t_out, executed_approval=executed)
            return CONTINUE

        # 1) guards
        if n >= self.s.max_steps:
            return self._finish(run, state, plan, n, t_in, t_out, {
                "root_cause": f"Agent stopped: step limit ({self.s.max_steps}) reached without a conclusion.",
                "severity": "SEV3", "confidence": 0.0, "follow_ups": ["Human to continue the investigation"]})
        if t_in + t_out >= self.s.max_run_tokens:
            return self._finish(run, state, plan, n, t_in, t_out, {
                "root_cause": f"Agent stopped: token budget ({self.s.max_run_tokens}) exhausted.",
                "severity": "SEV3", "confidence": 0.0, "follow_ups": ["Human to continue the investigation"]})

        # 2) context
        guidance = self._guidance(run_id)
        services = [s for s in state["world"]["services"] if s in run["alert"]]
        for s in state.get("steps", []):
            for c in s["calls"]:
                if c["tool"] == "list_services" and c.get("ok"):
                    services += [r["service"] for r in c["data"] if r["status"] != "healthy"]
        memories = self.db.search_memory(sorted(set(services)))
        prompt, layers = build_context(run, state, plan, guidance, memories, self.s)

        # 3) ask the model
        try:
            decision = self.llm.decide(SYSTEM_PROMPT, prompt, self.registry.schemas(),
                                       {**state, "plan": plan, "guidance": guidance})
        except LLMError as e:
            self._event(run_id, n, "error", message=str(e))
            self.db.set_status(run_id, "failed", ("running",), error=str(e))
            return STOP
        n += 1
        t_in, t_out = t_in + decision.tokens_in, t_out + decision.tokens_out
        cost = (decision.tokens_in / 1e6 * self.s.price_input_per_m
                + decision.tokens_out / 1e6 * self.s.price_output_per_m)
        self._event(run_id, n, "llm_call", model=decision.model, tokens_in=decision.tokens_in,
                    tokens_out=decision.tokens_out, latency_ms=decision.latency_ms, cost_usd=round(cost, 6),
                    context_layers=layers, memories_used=len(memories))
        if not decision.thought:  # some models skip the text part; still tell the operator what is happening
            decision.thought = "(no reasoning text from model) calling " + ", ".join(c.name for c in decision.calls)
        self._event(run_id, n, "thought", text=decision.thought)
        if t_in + t_out >= 0.8 * self.s.max_run_tokens:
            self._event(run_id, n, "warning", message="Over 80% of the run token budget used.")

        step_rec: dict = {"n": n, "thought": decision.thought, "calls": []}
        state.setdefault("steps", []).append(step_rec)
        if not decision.calls:
            state.setdefault("notes", []).append(f"Step {n}: you replied without a tool call. "
                                                 "Always call a tool or finish_triage.")
            self._checkpoint(run, state, plan, n, t_in, t_out)
            return CONTINUE

        # 4) dispatch
        previous = [json.dumps([c["tool"], c.get("args", {})], sort_keys=True)
                    for s in state["steps"][:-1] for c in s["calls"] if c.get("ok")]
        reads, risky = [], []
        for call in decision.calls:
            tool = self.registry.get(call.name)
            if call.name == "finish_triage":
                step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": True,
                                          "summary": "finished", "attempts": 1, "latency_ms": 0})
                return self._finish(run, state, plan, n, t_in, t_out, call.args)
            if call.name == "update_plan":
                new_plan = [str(x) for x in (call.args.get("steps") or [])][:10]
                kind = "plan_revised" if plan else "plan"
                plan = new_plan
                step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": True,
                                          "summary": f"plan with {len(plan)} steps", "attempts": 1, "latency_ms": 0})
                self._event(run_id, n, kind, steps=plan)
            elif tool is None:
                step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": False, "attempts": 0,
                                          "latency_ms": 0, "error": f"unknown tool '{call.name}'. "
                                          f"Available: {sorted(self.registry.tools)}",
                                          "summary": f"unknown tool {call.name}"})
                self._event(run_id, n, "warning", message=f"Model called unknown tool {call.name}")
            elif tool.risky:
                risky.append(call)
            elif previous.count(json.dumps([call.name, call.args], sort_keys=True)) >= 2:
                # loop guard: the same successful call twice already -> refuse a third time
                step_rec["calls"].append({"tool": call.name, "args": call.args, "ok": False, "attempts": 0,
                                          "latency_ms": 0, "summary": "blocked repeated call",
                                          "error": "loop guard: identical call already made twice; "
                                                   "use the earlier result or try something else"})
                self._event(run_id, n, "warning", message=f"Loop guard blocked repeated {call.name}")
            else:
                reads.append(call)

        for call in reads:
            self._event(run_id, n, "tool_call", tool=call.name, args=call.args, risky=False)
        if reads:  # read-only tools are safe to run in parallel; results are aggregated into one step
            with ThreadPoolExecutor(max_workers=min(4, len(reads))) as pool:
                results = list(pool.map(lambda c: self._run_tool(state["world"], c), reads))
            for rec in results:
                step_rec["calls"].append(rec)
                self._event(run_id, n, "tool_result", tool=rec["tool"], args=rec["args"], ok=rec["ok"],
                            summary=rec["summary"], error=rec.get("error"), attempts=rec["attempts"],
                            latency_ms=rec["latency_ms"], risky=False,
                            output=json.dumps(rec.get("data"), default=str)[:2000] if rec["ok"] else None)
                if rec.get("attempts", 1) > 1:
                    self._event(run_id, n, "retry", tool=rec["tool"], attempts=rec["attempts"], ok=rec["ok"])
                if rec.get("flagged"):
                    self._event(run_id, n, "warning", message=f"Possible prompt injection in {rec['tool']} output. "
                                "It is passed to the model as untrusted data and cannot bypass the approval gate.")

        for extra in risky[1:]:
            step_rec["calls"].append({"tool": extra.name, "args": extra.args, "ok": False, "attempts": 0,
                                      "latency_ms": 0, "risky": True, "summary": "not run",
                                      "error": "only one risky action per turn; propose it again later if needed"})
        if risky:  # human-in-the-loop gate (enforced in code, not prompt)
            call = risky[0]
            appr = self.db.create_approval(run_id, n, call.name, call.args, decision.thought or "")
            state["pending"] = {"approval_id": appr["id"], "tool": call.name, "args": call.args, "step": n}
            step_rec["calls"].append({"tool": call.name, "args": call.args, "risky": True, "ok": None,
                                      "awaiting_approval": True, "summary": "waiting for human approval"})
            self._checkpoint(run, state, plan, n, t_in, t_out)
            self._event(run_id, n, "approval_requested", approval_id=appr["id"], tool=call.name, args=call.args,
                        reason=decision.thought)
            self.db.set_status(run_id, "waiting_approval", ("running",))
            return STOP

        # 5) checkpoint
        self._checkpoint(run, state, plan, n, t_in, t_out)
        return CONTINUE
