"""Google Gemini adapter (official `google-genai` SDK, function calling).

Design choice: every step is a *single fresh request* built from the durable
state (see agent/context.py) instead of an ever-growing chat history. That keeps
the context small and deliberate, and makes resume-after-restart trivial
because nothing lives only in the SDK's memory.
"""
from __future__ import annotations

import os
import time

from app.llm.base import LLMDecision, LLMError, ToolCall


def to_function_declarations(tools: list[dict]):
    from google.genai import types
    return [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                      parameters_json_schema=t["parameters"]) for t in tools]


def parse_response(resp, model: str, latency_ms: int) -> LLMDecision:
    """Turn a GenerateContentResponse into our LLMDecision (pure function, unit-tested with fakes)."""
    cands = getattr(resp, "candidates", None) or []
    if not cands or not getattr(cands[0], "content", None):
        feedback = getattr(resp, "prompt_feedback", None)
        raise LLMError(f"Gemini returned no content (feedback={feedback})")
    thoughts, calls = [], []
    for part in cands[0].content.parts or []:
        fc = getattr(part, "function_call", None)
        if fc is not None and getattr(fc, "name", None):
            calls.append(ToolCall(name=fc.name, args=dict(fc.args or {})))
        elif getattr(part, "text", None):
            thoughts.append(part.text.strip())
    usage = getattr(resp, "usage_metadata", None)
    t_in = getattr(usage, "prompt_token_count", 0) or 0
    t_out = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
    return LLMDecision(thought=" ".join(thoughts)[:1000], calls=calls, tokens_in=t_in, tokens_out=t_out,
                       latency_ms=latency_ms, model=model)


class GeminiLLM:
    name = "gemini"

    def __init__(self, api_key: str, model: str, max_retries: int = 3, backoff: float = 2.0, client=None):
        self.model = model
        self.max_retries = max_retries
        self.backoff = backoff
        if client is None:
            from google import genai
            from google.genai import types
            api_version = os.getenv("GEMINI_API_VERSION", "v1beta")
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version=api_version))
        self.client = client

    def decide(self, system: str, prompt: str, tools: list[dict], state: dict) -> LLMDecision:
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.1,
            tools=[types.Tool(function_declarations=to_function_declarations(tools))],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
                return parse_response(resp, self.model, int((time.perf_counter() - started) * 1000))
            except LLMError:
                raise
            except Exception as e:  # 429 / 5xx / network: retry with exponential backoff
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
        raise LLMError(f"Gemini call failed after {self.max_retries} attempts: {last_err}")
