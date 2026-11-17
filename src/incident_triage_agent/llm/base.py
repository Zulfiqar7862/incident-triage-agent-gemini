"""The one interface the agent loop needs from a model: "given this context, what next?"."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class LLMDecision:
    thought: str                      # short reasoning summary shown to the operator
    calls: list[ToolCall]             # zero or more tool calls (several = run in parallel)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    model: str = ""


class LLMError(Exception):
    """The model could not be reached / answered after retries. The run fails but can be resumed."""


class LLM(Protocol):
    name: str

    def decide(self, system: str, prompt: str, tools: list[dict], state: dict) -> LLMDecision:
        """`prompt` is the rendered context for real models; `state` is the structured
        checkpoint (used by the offline mock policy, ignored by Gemini)."""
        ...
