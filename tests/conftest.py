"""Shared fixtures. Tests never use a real API key: they force mock/scripted LLMs."""
from __future__ import annotations

import pytest

from app.agent.loop import Agent
from app.agent.runtime import Runtime
from app.config import Settings
from app.db import DB
from app.llm.base import LLMDecision, ToolCall
from app.llm.mock import MockLLM
from app.tools.infra_tools import build_registry


def make_settings(db_path: str, **kw) -> Settings:
    base = dict(gemini_api_key="", db_path=db_path, step_delay=0, tool_backoff=0)
    base.update(kw)
    return Settings(**base)


def make_runtime(db_path: str, llm=None, **kw) -> tuple[Runtime, DB]:
    s = make_settings(db_path, **kw)
    db = DB(db_path)
    return Runtime(db, Agent(db, llm or MockLLM(), build_registry(), s, sleep=lambda _: None), s), db


class ScriptedLLM:
    """Returns pre-written decisions (or calls a function) - for testing edge cases."""
    name = "scripted"

    def __init__(self, script):
        self.script, self.i = script, 0

    def decide(self, system, prompt, tools, state):
        if callable(self.script):
            d = self.script(state)
        else:
            d = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(d, Exception):
            raise d
        return d


def call(name, **args) -> ToolCall:
    return ToolCall(name, args)


def decision(*calls, thought="t") -> LLMDecision:
    return LLMDecision(thought=thought, calls=list(calls), tokens_in=100, tokens_out=20, model="scripted")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "agent.db")
