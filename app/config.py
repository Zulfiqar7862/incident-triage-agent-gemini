"""All settings in one place, read from environment variables (and .env).

Keeping config in a single frozen dataclass makes it easy to see every knob
and to override it in tests (`Settings(step_delay=0)`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: str) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/agent.db"))

    max_steps: int = field(default_factory=lambda: _i("MAX_STEPS", "15"))
    max_run_tokens: int = field(default_factory=lambda: _i("MAX_RUN_TOKENS", "200000"))
    step_delay: float = field(default_factory=lambda: _f("STEP_DELAY_SECONDS", "0.8"))
    tool_max_retries: int = field(default_factory=lambda: _i("TOOL_MAX_RETRIES", "3"))
    tool_backoff: float = field(default_factory=lambda: _f("TOOL_BACKOFF_SECONDS", "0.3"))
    min_confidence: float = field(default_factory=lambda: _f("MIN_CONFIDENCE", "0.6"))

    # Context budget (characters; ~4 chars per token)
    tool_output_chars: int = 1500      # each tool result is truncated to this
    recent_steps_full: int = 4         # last N steps shown in full, older ones summarized
    context_char_budget: int = 24000   # hard cap on the whole prompt (~6k tokens)

    price_input_per_m: float = field(default_factory=lambda: _f("PRICE_INPUT_PER_M", "0.30"))
    price_output_per_m: float = field(default_factory=lambda: _f("PRICE_OUTPUT_PER_M", "2.50"))

    @property
    def llm_mode(self) -> str:
        return "gemini" if self.gemini_api_key else "mock"
