"""The Gemini path can't be called in CI without a key, so we test it with fake SDK objects."""
from types import SimpleNamespace as NS

import pytest

from incident_triage_agent.llm.base import LLMError
from incident_triage_agent.llm.gemini import GeminiLLM, parse_response, retry_delay, to_function_declarations
from incident_triage_agent.tools.infra_tools import build_registry


def fake_response():
    parts = [NS(text="Error rate spiked after deploy; checking logs.", function_call=None),
             NS(text=None, function_call=NS(name="get_logs", args={"service": "payments-api"})),
             NS(text=None, function_call=NS(name="get_metrics", args={"service": "payments-api"}))]
    return NS(candidates=[NS(content=NS(parts=parts))],
              usage_metadata=NS(prompt_token_count=1200, candidates_token_count=40, thoughts_token_count=10))


def test_parse_response_extracts_thought_parallel_calls_and_usage():
    d = parse_response(fake_response(), "gemini-x", 321)
    assert d.thought.startswith("Error rate") and [c.name for c in d.calls] == ["get_logs", "get_metrics"]
    assert (d.tokens_in, d.tokens_out, d.latency_ms) == (1200, 50, 321)


def test_function_declarations_built_from_registry():
    decls = to_function_declarations(build_registry().schemas())
    assert {d.name for d in decls} >= {"get_logs", "rollback_deployment", "finish_triage"}


class FakeModels:
    def __init__(self, failures):
        self.failures, self.calls, self.kw = failures, 0, None

    def generate_content(self, **kw):
        self.calls += 1
        self.kw = kw
        if self.calls <= self.failures:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return fake_response()


def test_gemini_retries_transient_errors_then_succeeds():
    models = FakeModels(failures=2)
    llm = GeminiLLM("k", "gemini-x", backoff=0, client=NS(models=models))
    d = llm.decide("sys", "prompt", build_registry().schemas(), {})
    assert models.calls == 3 and len(d.calls) == 2
    cfg = models.kw["config"]
    assert cfg.system_instruction == "sys" and cfg.tools[0].function_declarations


def test_gemini_gives_up_with_llm_error():
    llm = GeminiLLM("k", "gemini-x", backoff=0, client=NS(models=FakeModels(failures=9)))
    with pytest.raises(LLMError):
        llm.decide("sys", "prompt", [], {})


def test_empty_candidates_raise():
    with pytest.raises(LLMError):
        parse_response(NS(candidates=[], prompt_feedback="blocked"), "m", 1)


def test_rate_limit_hint_is_honoured():
    err = RuntimeError("429 RESOURCE_EXHAUSTED ... Please retry in 47.7s. {'retryDelay': '47s'}")
    assert retry_delay(err, 1, 2.0) == 48.7
    assert retry_delay(RuntimeError("503"), 3, 2.0) == 8.0
    assert retry_delay(RuntimeError("retry in 500s"), 1, 2.0) == 60.0  # capped
