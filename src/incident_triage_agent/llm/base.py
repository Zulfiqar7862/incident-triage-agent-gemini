"""The one interface the agent loop needs from a model: "given this context, what next?"."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

OnText = Callable[[str], None]   # receives model text as it streams in


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


def chunks(text: str, words: int = 3) -> list[str]:
    """Split text into small pieces that concatenate back to exactly the same text."""
    parts = re.findall(r"\s*\S+\s*", text)
    return ["".join(parts[i:i + words]) for i in range(0, len(parts), words)]


class LLMError(Exception):
    """The model could not be reached / answered after retries. The run fails but can be resumed."""


class LLM(Protocol):
    name: str

    def decide(self, system: str, prompt: str, tools: list[dict], state: dict,
               on_text: OnText | None = None) -> LLMDecision:
        """`prompt` is the rendered context for real models; `state` is the structured
        checkpoint (used by the offline mock policy, ignored by Gemini). `on_text`, if given,
        is called with each piece of reasoning text as it streams in."""
        ...
