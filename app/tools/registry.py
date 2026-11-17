"""Tool registry: structured schemas, argument validation, retry with backoff.

Every tool has:
  * a JSON schema (sent to the LLM as a function declaration),
  * a `risky` flag - risky (mutating) tools NEVER run without human approval.
    This rule lives in code, not in the prompt, so prompt injection can't bypass it.
  * a python function `fn(world, **args) -> ToolResult`.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """Permanent error (bad input, not found). Retrying will not help; the LLM should self-correct."""


class TransientToolError(Exception):
    """Temporary error (timeout, 503). Safe to retry with backoff."""


@dataclass
class ToolResult:
    summary: str                      # one line, used when older steps are compacted
    data: Any = None                  # full payload (truncated before it reaches the LLM)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., ToolResult]
    risky: bool = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters, "risky": self.risky}


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def validate_args(tool: Tool, args: dict) -> dict:
    """Small JSON-schema check: required fields, types, enums. Raises ToolError."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props = tool.parameters.get("properties", {})
    for req in tool.parameters.get("required", []):
        if req not in args:
            raise ToolError(f"missing required argument '{req}'")
    clean = {}
    for k, v in args.items():
        if k not in props:
            raise ToolError(f"unknown argument '{k}' (allowed: {sorted(props)})")
        spec = props[k]
        t = spec.get("type")
        if t == "integer" and isinstance(v, float) and v.is_integer():
            v = int(v)  # LLMs often send 6.0
        if t and not isinstance(v, _TYPES[t]):
            raise ToolError(f"argument '{k}' must be {t}")
        if "enum" in spec and v not in spec["enum"]:
            raise ToolError(f"argument '{k}' must be one of {spec['enum']}")
        clean[k] = v
    return clean


def execute_with_retry(tool: Tool, world: dict, args: dict, *, max_retries: int = 3,
                       backoff: float = 0.3, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run a tool. Transient errors are retried with exponential backoff (backoff * 2^n).

    Returns an observation dict - never raises - so the agent loop can show the
    error to the LLM and let it self-correct.
    """
    started = time.perf_counter()
    attempts = 0
    try:
        clean = validate_args(tool, args)
    except ToolError as e:
        return {"ok": False, "error": f"invalid arguments: {e}", "summary": f"{tool.name} rejected: {e}",
                "attempts": 0, "latency_ms": 0}
    while True:
        attempts += 1
        try:
            res = tool.fn(world, **clean)
            return {"ok": True, "summary": res.summary, "data": res.data, "attempts": attempts,
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
        except TransientToolError as e:
            if attempts > max_retries:
                return {"ok": False, "error": f"transient error, gave up after {attempts} attempts: {e}",
                        "summary": f"{tool.name} failed after {attempts} attempts", "attempts": attempts,
                        "latency_ms": int((time.perf_counter() - started) * 1000)}
            sleep(backoff * (2 ** (attempts - 1)))
        except ToolError as e:
            return {"ok": False, "error": str(e), "summary": f"{tool.name} error: {e}", "attempts": attempts,
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
