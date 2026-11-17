"""Planner: decides the next step.

Builds the context from durable state (context.py), recalls long-term memory
for the services involved, and asks the model for its next decision: a plan,
tool calls, or finish_triage. Until the run has a plan, the only tool offered is
update_plan, so every run starts with a visible plan. Raises LLMError if the model is unavailable.
"""
from __future__ import annotations

from incident_triage_agent.agent.context import build_context
from incident_triage_agent.agent.prompts import SYSTEM_PROMPT
from incident_triage_agent.agent.state import RunState
from incident_triage_agent.config import Settings
from incident_triage_agent.db import DB
from incident_triage_agent.llm.base import LLM, LLMDecision, OnText
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

    def next_decision(self, rs: RunState, guidance: list[str],
                      on_text: OnText | None = None) -> tuple[LLMDecision, dict, list]:
        """Return (decision, context layer sizes, memories used)."""
        memories = self.db.search_memory(self.services_in_scope(rs))
        prompt, layers = build_context(rs.run, rs.state, rs.plan, guidance, memories, self.s)
        tools = self.registry.schemas()
        if not rs.plan:   # plan first: until a plan exists the model is offered only update_plan
            tools = [t for t in tools if t["name"] == "update_plan"]
        decision = self.llm.decide(SYSTEM_PROMPT, prompt, tools,
                                   {**rs.state, "plan": rs.plan, "guidance": guidance}, on_text)
        return decision, layers, memories
