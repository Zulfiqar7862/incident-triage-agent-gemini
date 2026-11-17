"""Planner: decides the next step.

Builds the context from durable state (context.py), recalls long-term memory
for the services involved, and asks the model for its next decision: a plan,
tool calls, or finish_triage. Raises LLMError if the model is unavailable.
"""
from __future__ import annotations

from incident_triage_agent.agent.context import build_context
from incident_triage_agent.agent.prompts import SYSTEM_PROMPT
from incident_triage_agent.agent.state import RunState
from incident_triage_agent.config import Settings
from incident_triage_agent.db import DB
from incident_triage_agent.llm.base import LLM, LLMDecision
from incident_triage_agent.tools.registry import ToolRegistry


class Planner:
    def __init__(self, db: DB, llm: LLM, registry: ToolRegistry, settings: Settings):
        self.db, self.llm, self.registry, self.s = db, llm, registry, settings

    @staticmethod
    def services_in_scope(rs: RunState) -> list[str]:
        """Services named in the alert plus any that list_services reported unhealthy."""
        services = [s for s in rs.state["world"]["services"] if s in rs.run["alert"]]
        for s in rs.state.get("steps", []):
            for c in s["calls"]:
                if c["tool"] == "list_services" and c.get("ok"):
                    services += [r["service"] for r in c["data"] if r["status"] != "healthy"]
        return sorted(set(services))

    def next_decision(self, rs: RunState, guidance: list[str]) -> tuple[LLMDecision, dict, list]:
        """Return (decision, context layer sizes, memories used)."""
        memories = self.db.search_memory(self.services_in_scope(rs))
        prompt, layers = build_context(rs.run, rs.state, rs.plan, guidance, memories, self.s)
        decision = self.llm.decide(SYSTEM_PROMPT, prompt, self.registry.schemas(),
                                   {**rs.state, "plan": rs.plan, "guidance": guidance})
        return decision, layers, memories
