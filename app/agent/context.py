"""Context engineering: decide exactly what the model sees at each step.

The prompt is rebuilt from durable state on every step (no hidden chat history).
Layers, in priority order:
  incident  - the alert (always)
  memory    - up to 3 similar past incidents from long-term memory (may be stale)
  plan      - the current plan
  guidance  - operator messages (trusted, high priority)
  history   - older steps compacted to one-line summaries, last N steps in full
  notes     - loop-guard / error notes for self-correction
Budgets: each tool output is truncated, and if the whole prompt is still over
budget we show fewer steps in full (they fall back to their summaries). Nothing is
dropped silently: every step keeps at least its one-line summary.
"""
from __future__ import annotations

import json

from app.config import Settings


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _render_call_full(c: dict, limit: int) -> str:
    head = f"- {c['tool']}({json.dumps(c.get('args', {}))})"
    if not c.get("ok"):
        return f"{head} -> ERROR: {c.get('error')}"
    body = json.dumps(c.get("data"), default=str).replace("</tool_output>", "</tool_output_>")
    flag = ' flagged="possible-prompt-injection"' if c.get("flagged") else ""
    return (f"{head} -> {c.get('summary')}\n"
            f"<tool_output tool=\"{c['tool']}\" trust=\"untrusted\"{flag}>{_truncate(body, limit)}</tool_output>")


def _render_call_short(c: dict) -> str:
    status = c.get("summary") if c.get("ok") else f"ERROR: {c.get('error')}"
    return f"{c['tool']}({json.dumps(c.get('args', {}))}) -> {status}"


def _history(steps: list[dict], recent_n: int, output_chars: int) -> str:
    """Older steps as one-line summaries, the last `recent_n` steps in full."""
    older, recent = (steps[:-recent_n], steps[-recent_n:]) if recent_n else (steps, [])
    hist = []
    if older:
        hist.append("Earlier steps (compacted):")
        for s in older:
            hist.append(f"  step {s['n']}: " + "; ".join(_render_call_short(c) for c in s["calls"]))
    for s in recent:
        hist.append(f"Step {s['n']} - your reasoning: {s.get('thought') or '(none)'}")
        hist.extend(_render_call_full(c, output_chars) for c in s["calls"])
    return "## WORK SO FAR\n" + ("\n".join(hist) if hist else "(nothing yet)")


def _layers(run: dict, state: dict, plan: list[str], guidance: list[str], memories: list[dict],
            settings: Settings, recent_n: int) -> dict[str, str]:
    """All prompt layers in priority order, with `recent_n` steps shown in full."""
    layers: dict[str, str] = {"incident": f"## INCIDENT\nTitle: {run['title']}\nAlert: {run['alert']}"}
    if memories:
        mem = "\n".join(f"- [{m['service']}] {m['root_cause']} -> fixed by: {m['resolution']}" for m in memories)
        layers["memory"] = "## SIMILAR PAST INCIDENTS (long-term memory, may be outdated - verify)\n" + mem
    layers["plan"] = "## CURRENT PLAN\n" + ("\n".join(f"{i + 1}. {p}" for i, p in enumerate(plan))
                                            if plan else "(no plan yet - call update_plan first)")
    if guidance:
        layers["guidance"] = "## OPERATOR GUIDANCE (trusted human, follow it)\n" + "\n".join(
            f"- {g}" for g in guidance[-5:])
    layers["history"] = _history(state.get("steps", []), recent_n, settings.tool_output_chars)
    notes = state.get("notes", [])[-5:]
    if notes:
        layers["notes"] = "## NOTES FROM THE RUNTIME\n" + "\n".join(f"- {n}" for n in notes)
    layers["task"] = (f"## YOUR TASK NOW\nStep {run['step'] + 1} of max {settings.max_steps}. "
                      "Explain your reasoning briefly, then call the next tool(s) or finish_triage.")
    return layers


def build_context(run: dict, state: dict, plan: list[str], guidance: list[str], memories: list[dict],
                  settings: Settings) -> tuple[str, dict]:
    """Return (prompt_text, layer_stats). layer_stats = approx tokens per layer, for the trace/UI."""
    steps = state.get("steps", [])
    n = min(settings.recent_steps_full, len(steps))
    layers = _layers(run, state, plan, guidance, memories, settings, n)
    while n > 0 and sum(len(v) for v in layers.values()) > settings.context_char_budget:
        n -= 1                        # compaction: show one fewer step in full
        layers = _layers(run, state, plan, guidance, memories, settings, n)
    prompt = "\n\n".join(layers.values())
    if len(prompt) > settings.context_char_budget:  # last resort hard cap (keeps the task at the end)
        prompt = _truncate(prompt, settings.context_char_budget - 400) + "\n\n" + layers["task"]
    stats = {k: len(v) // 4 for k, v in layers.items()}
    stats["total_tokens_est"] = len(prompt) // 4
    stats["steps_in_full"] = n
    stats["steps_compacted"] = len(steps) - n
    return prompt, stats
