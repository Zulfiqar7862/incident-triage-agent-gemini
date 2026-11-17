"""Human-in-the-loop: the approval gate and confidence routing.

Risk policy: a tool registered with `risky=True` changes production. The agent
never runs it directly; the gate records a pending approval and the run waits.
When the operator decides, `resolve` turns the decision into a step record.
Routing decides, in code, whether a finished run is `completed` or goes to a human.
"""
from __future__ import annotations

import json

from incident_triage_agent.agent.state import RunState
from incident_triage_agent.db import DB
from incident_triage_agent.llm.base import ToolCall


class ApprovalGate:
    def __init__(self, db: DB):
        self.db = db

    def request(self, rs: RunState, call: ToolCall, reason: str) -> dict:
        """Create a pending approval for `call` and remember it in the run state."""
        appr = self.db.create_approval(rs.run_id, rs.step, call.name, call.args, reason)
        rs.state["pending"] = {"approval_id": appr["id"], "tool": call.name, "args": call.args, "step": rs.step}
        return appr

    def decision(self, rs: RunState) -> tuple[str, dict | None]:
        """Status of the pending approval: pending, approved, rejected or cancelled (if it vanished)."""
        appr = self.db.get_approval(rs.state["pending"]["approval_id"])
        return (appr["status"] if appr else "cancelled"), appr

    @staticmethod
    def rejected_record(rs: RunState, status: str, appr: dict | None) -> dict:
        """Step record for a rejected/cancelled action, plus a note so the model adapts instead of retrying."""
        pending = rs.state["pending"]
        comment = (appr or {}).get("comment") or "no comment"
        rs.state.setdefault("notes", []).append(
            f"The operator {status} {pending['tool']}({json.dumps(pending['args'])}): {comment}. "
            "Do not retry the same action; adapt or finish.")
        return {"tool": pending["tool"], "args": pending["args"], "risky": True, "ok": False,
                "error": f"Operator {status} this action: {comment}",
                "summary": f"{pending['tool']} {status} by operator", "attempts": 0, "latency_ms": 0,
                "approval": {"id": pending["approval_id"], "status": status, "comment": comment}}

    @staticmethod
    def close(rs: RunState, rec: dict) -> None:
        """Replace the 'waiting for approval' placeholder in its step with the final record."""
        pending = rs.state["pending"]
        step_rec = next(s for s in rs.state["steps"] if s["n"] == pending["step"])
        step_rec["calls"] = [c for c in step_rec["calls"] if not c.get("awaiting_approval")] + [rec]
        rs.state["pending"] = None


def route(confidence: float, mitigated: bool, min_confidence: float) -> tuple[str, str]:
    """Routing rule (in code, not left to the model).

    completed = a fix was applied (human-approved) AND the model is confident
    escalated = low confidence, or nothing was mitigated -> a human must look
    Returns (status, reason); reason is empty when completed.
    """
    if confidence < min_confidence:
        reason = f"confidence {confidence:.2f} below {min_confidence}"
    elif not mitigated:
        reason = "no mitigation was applied; a human should confirm and close"
    else:
        reason = ""
    return ("escalated" if reason else "completed"), reason
