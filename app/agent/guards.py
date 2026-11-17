"""Guards: hard limits checked in code, whatever the model decides.

- step limit and token budget: stop the run with a low-confidence report (goes to a human)
- loop guard: refuse a read-only call that already succeeded twice with the same arguments
"""
from __future__ import annotations

import json

from app.agent.state import RunState
from app.config import Settings
from app.llm.base import ToolCall

TOKEN_WARNING_RATIO = 0.8  # warn the operator once a run has used this share of its token budget


def limit_reached(rs: RunState, s: Settings) -> dict | None:
    """Return a stop report if the run hit its step limit or token budget, else None."""
    if rs.step >= s.max_steps:
        reason = f"Agent stopped: step limit ({s.max_steps}) reached without a conclusion."
    elif rs.tokens >= s.max_run_tokens:
        reason = f"Agent stopped: token budget ({s.max_run_tokens}) exhausted."
    else:
        return None
    return {"root_cause": reason, "severity": "SEV3", "confidence": 0.0,
            "follow_ups": ["Human to continue the investigation"]}


def near_token_budget(rs: RunState, s: Settings) -> bool:
    return rs.tokens >= TOKEN_WARNING_RATIO * s.max_run_tokens


def _key(tool: str, args: dict) -> str:
    return json.dumps([tool, args], sort_keys=True)


def is_repeated(steps: list[dict], call: ToolCall) -> bool:
    """True if this exact call already succeeded twice in earlier steps (the last step is the current one)."""
    previous = [_key(c["tool"], c.get("args", {})) for s in steps[:-1] for c in s["calls"] if c.get("ok")]
    return previous.count(_key(call.name, call.args)) >= 2
