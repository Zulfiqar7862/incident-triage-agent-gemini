"""Streaming: live deltas add up to exactly the text that is saved, and arrive plan first, answer last."""
from types import SimpleNamespace as NS

from incident_triage_agent.llm.gemini import collect_stream
from tests.agent.test_plan_answer import finished_run


def deltas(rt, run_id, kind):
    return [d for d in rt.agent.tokens.since(run_id) if d["kind"] == kind]


def test_thought_deltas_concatenate_to_each_saved_thought(db_path):
    rt, db, run = finished_run(db_path)
    for e in (e for e in db.events(run["id"]) if e["type"] == "thought"):
        streamed = "".join(d["text"] for d in deltas(rt, run["id"], "thought") if d["step"] == e["step"])
        assert streamed == e["data"]["text"]


def test_plan_and_answer_deltas_match_saved_events(db_path):
    rt, db, run = finished_run(db_path)
    evs = {e["type"]: e["data"] for e in db.events(run["id"])}
    assert "".join(d["text"] for d in deltas(rt, run["id"], "plan")) == "\n".join(evs["plan"]["steps"])
    assert "".join(d["text"] for d in deltas(rt, run["id"], "answer")) == evs["answer"]["diagnosis"]


def test_stream_order_plan_before_answer(db_path):
    rt, _, run = finished_run(db_path)
    kinds = [d["kind"] for d in rt.agent.tokens.since(run["id"])]
    assert kinds.index("plan") < kinds.index("answer") and kinds[-1] == "answer"


def test_gemini_stream_forwards_text_and_keeps_function_calls():
    chunk = lambda *parts: NS(candidates=[NS(content=NS(parts=list(parts)))], usage_metadata=None)  # noqa: E731
    stream = [chunk(NS(text="Checking ", function_call=None)), chunk(NS(text="logs.", function_call=None)),
              chunk(NS(text=None, function_call=NS(name="get_logs", args={"service": "a"})))]
    seen = []
    resp = collect_stream(iter(stream), seen.append)
    assert seen == ["Checking ", "logs."] and len(resp.candidates[0].content.parts) == 3
