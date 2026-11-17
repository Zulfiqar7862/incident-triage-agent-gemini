"""Final answer: the run's conclusion in four plain fields, shown at the end of a run.

Built only from the final report that routing already produced, so it cannot change
whether a run is completed or escalated. Pure function: the same report always gives
the same answer, so it survives restarts without being stored twice.
"""
from __future__ import annotations

from pydantic import BaseModel


class Answer(BaseModel):
    diagnosis: str      # the root cause in one sentence
    action_taken: str   # what was changed in production (or "No change was made")
    outcome: str        # fixed and verified, or handed to a human and why
    confidence: float   # 0-1, as reported by the agent


def build_answer(status: str, report: dict) -> Answer:
    applied = report.get("actions_applied") or []
    actions = "; ".join(report.get("actions_taken") or [])
    if status == "completed":
        outcome = f"Fixed and verified: {report.get('routing_reason', '')}"
    else:
        outcome = f"Handed to a human: {report.get('routing_reason', '')}"
    return Answer(diagnosis=str(report.get("root_cause") or "Unknown"),
                  action_taken=actions if applied and actions else "No change was made",
                  outcome=outcome.rstrip(": "),
                  confidence=float(report.get("confidence") or 0))
