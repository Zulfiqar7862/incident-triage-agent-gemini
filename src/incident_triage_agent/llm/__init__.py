"""LLM adapters. `make_llm(settings)` picks Gemini when a key is set, otherwise the offline mock."""
from __future__ import annotations

from incident_triage_agent.config import Settings
from incident_triage_agent.llm.base import LLM, LLMDecision, LLMError, ToolCall


def make_llm(settings: Settings) -> LLM:
    if settings.gemini_api_key:
        from incident_triage_agent.llm.gemini import GeminiLLM
        return GeminiLLM(api_key=settings.gemini_api_key, model=settings.gemini_model)
    from incident_triage_agent.llm.mock import MockLLM
    return MockLLM()


__all__ = ["LLM", "LLMDecision", "LLMError", "ToolCall", "make_llm"]
