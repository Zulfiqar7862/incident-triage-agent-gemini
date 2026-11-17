"""Executor: runs tool calls through the registry.

Arguments are validated against the tool's JSON schema, transient failures are
retried with exponential backoff (registry.execute_with_retry), read-only calls
run in parallel, and output that looks like a prompt injection is flagged.
Outputs are truncated later, when the context is built (context.py).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from incident_triage_agent.agent.safety import looks_like_injection
from incident_triage_agent.config import Settings
from incident_triage_agent.llm.base import ToolCall
from incident_triage_agent.tools.registry import ToolRegistry, execute_with_retry

MAX_PARALLEL_TOOLS = 4


class Executor:
    def __init__(self, registry: ToolRegistry, settings: Settings, sleep: Callable[[float], None] | None = None):
        self.registry, self.s = registry, settings
        self.sleep = sleep or time.sleep

    def run(self, world: dict, call: ToolCall) -> dict:
        """Run one call and return its step record: tool, args, risky, ok, data/error, summary, attempts, latency."""
        tool = self.registry.get(call.name)
        assert tool is not None
        obs = execute_with_retry(tool, world, call.args, max_retries=self.s.tool_max_retries,
                                 backoff=self.s.tool_backoff, sleep=self.sleep)
        rec = {"tool": call.name, "args": call.args, "risky": tool.risky, **obs}
        if obs.get("ok") and looks_like_injection(obs.get("data")):
            rec["flagged"] = True
        return rec

    def run_parallel(self, world: dict, calls: list[ToolCall]) -> list[dict]:
        """Read-only tools are safe to run in parallel; results come back in call order."""
        if not calls:
            return []
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TOOLS, len(calls))) as pool:
            return list(pool.map(lambda c: self.run(world, c), calls))
