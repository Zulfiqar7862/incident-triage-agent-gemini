"""Deterministic offline "LLM": a rule-based triage policy.

It exists so that the whole app (UI, approvals, resume, tests, evals) works with
no API key and no internet, and so tests are deterministic. It reads the same
structured state the real model sees (tool results, approvals, guidance) and
reacts to it - including tool failures and operator rejections - so it
exercises every path of the agent loop. It is NOT meant to be smart; Gemini is.
"""
from __future__ import annotations

import json
import time

from app.llm.base import LLMDecision, ToolCall

PLAN = [
    "List services and find which ones are unhealthy",
    "Collect metrics, logs and recent deployments for the unhealthy service (in parallel)",
    "Search runbooks for the matching symptoms",
    "Form a root-cause hypothesis and propose the smallest safe mitigation (needs approval)",
    "Verify the mitigation with fresh metrics",
    "Write the incident report",
]


def _calls(state: dict) -> list[dict]:
    return [c for s in state.get("steps", []) for c in s.get("calls", [])]


def _match(c: dict, tool: str, args: dict) -> bool:
    return c["tool"] == tool and all(c.get("args", {}).get(k) == v for k, v in args.items())


def _last_ok(state: dict, tool: str, **args) -> dict | None:
    for c in reversed(_calls(state)):
        if _match(c, tool, args) and c.get("ok"):
            return c
    return None


def _failures(state: dict, tool: str, **args) -> int:
    return sum(1 for c in _calls(state) if _match(c, tool, args) and not c.get("ok"))


class MockLLM:
    name = "mock"
    model = "mock-policy-v1"

    def decide(self, system: str, prompt: str, tools: list[dict], state: dict) -> LLMDecision:
        started = time.perf_counter()
        thought, calls = self._policy(state)
        out = json.dumps({"thought": thought, "calls": [c.__dict__ for c in calls]})
        return LLMDecision(thought=thought, calls=calls, tokens_in=len(system + prompt) // 4,
                           tokens_out=len(out) // 4, latency_ms=int((time.perf_counter() - started) * 1000),
                           model=self.model)

    # ------------------------------------------------------------------
    def _policy(self, state: dict) -> tuple[str, list[ToolCall]]:
        prefix = ""
        if state.get("guidance"):
            prefix = f"Operator guidance noted ('{state['guidance'][-1][:80]}'). "
        if any(c.get("flagged") for c in _calls(state)):
            prefix += "Ignoring instructions found inside tool output (treated as data). "

        if not state.get("plan"):
            return "Starting triage: writing an investigation plan first.", [ToolCall("update_plan", {"steps": PLAN})]

        listing = _last_ok(state, "list_services")
        if not listing:
            return prefix + "First, find out which services are unhealthy.", [ToolCall("list_services")]

        unhealthy = [r["service"] for r in listing["data"] if r["status"] != "healthy"]
        if not unhealthy:
            if not _last_ok(state, "search_runbooks"):
                return (prefix + "Every service reports healthy. Checking the general runbook before concluding.",
                        [ToolCall("search_runbooks", {"query": "general triage nothing unhealthy"})])
            return (prefix + "No unhealthy service found, so I will not invent a fix. Handing back to a human.",
                    [self._finish("Could not confirm the alert: all services healthy, metrics normal.", "SEV4", [],
                                  [], ["Ask support for affected customer, URL and time",
                                       "Check client-side / CDN monitoring"], 0.35)])
        target = unhealthy[0]

        missing = [t for t in ("get_metrics", "get_logs", "get_recent_deployments")
                   if not _last_ok(state, t, service=target) and _failures(state, t, service=target) < 2]
        if missing:
            retry_note = " (retrying a tool that failed before)" if any(
                _failures(state, t, service=target) for t in missing) else ""
            return (prefix + f"{target} is unhealthy. Gathering {', '.join(missing)} in parallel{retry_note}.",
                    [ToolCall(t, {"service": target}) for t in missing])

        metrics = (_last_ok(state, "get_metrics", service=target) or {}).get("data") or {}
        logs = (_last_ok(state, "get_logs", service=target) or {}).get("data") or []
        deps = (_last_ok(state, "get_recent_deployments", service=target) or {}).get("data") or []
        cur = metrics.get("current", {})

        diagnosis = self._diagnose(target, cur, logs, deps, listing["data"])
        if not _last_ok(state, "search_runbooks"):
            return (prefix + f"Hypothesis: {diagnosis['root_cause']} Checking runbooks to confirm the fix.",
                    [ToolCall("search_runbooks", {"query": diagnosis["query"]})])

        if diagnosis["action"] is None:
            return (prefix + "Symptoms do not match a known pattern; escalating.",
                    [self._finish(diagnosis["root_cause"], "SEV3", [target], [], ["Manual investigation"], 0.4)])

        actions = [c for c in _calls(state) if c.get("risky")]
        if not actions:
            tool, args = diagnosis["action"]
            return (prefix + f"Runbook agrees. Proposing {tool} {args} - this changes production so it needs approval.",
                    [ToolCall(tool, args)])

        last = actions[-1]
        if not last.get("ok"):
            reason = last.get("error", "failed")
            return (prefix + f"The mitigation did not happen ({reason}). Leaving the decision to a human.",
                    [self._finish(diagnosis["root_cause"], diagnosis["severity"], [target], [],
                                  [f"Mitigation not applied: {reason}", *diagnosis["follow_ups"]], 0.5)])

        after = [c for c in _calls(state) if _match(c, "get_metrics", {"service": target})]
        verified = after and after[-1] is not None and _calls(state).index(after[-1]) > _calls(state).index(last)
        if not verified:
            return prefix + "Mitigation applied. Verifying with fresh metrics.", [
                ToolCall("get_metrics", {"service": target})]
        now = after[-1].get("data", {}).get("current", {})
        healthy = now.get("error_rate_pct", 99) < 1 and now.get("cpu_pct", 99) < 70 and now.get("memory_pct", 99) < 80
        return (prefix + ("Metrics are back to normal. Writing the report." if healthy else
                          "Metrics are still bad after the fix; escalating."),
                [self._finish(diagnosis["root_cause"], diagnosis["severity"], [target], [last["summary"]],
                              diagnosis["follow_ups"], 0.9 if healthy else 0.45)])

    @staticmethod
    def _diagnose(svc: str, m: dict, logs: list[str], deps: list[dict], listing: list[dict]) -> dict:
        recent = [d for d in deps if d["minutes_ago"] <= 60]
        errors = [line for line in logs if line.startswith("ERROR")]
        replicas = next((r["replicas"] for r in listing if r["service"] == svc), 2)
        if recent and m.get("error_rate_pct", 0) > 5:
            d = recent[0]
            return {"root_cause": f"Deploy {d['version']} of {svc} ({d['minutes_ago']} min ago, '{d['change']}') "
                                  f"introduced errors: {errors[0] if errors else 'error spike'}.",
                    "severity": "SEV2", "query": "error rate spike after deploy rollback",
                    "action": ("rollback_deployment", {"service": svc, "to_version": d["previous_version"]}),
                    "follow_ups": [f"Fix the bug in {d['version']} and add a test", "Add canary analysis to deploys"]}
        if any("OOMKilled" in line for line in logs) or m.get("memory_pct", 0) > 90:
            change = deps[0]["change"] if deps else "unknown change"
            return {"root_cause": f"Memory leak in {svc}: memory {m.get('memory_pct')}%, pods OOMKilled "
                                  f"(likely unbounded cache from '{change}').",
                    "severity": "SEV3", "query": "memory leak oomkilled restart",
                    "action": ("restart_service", {"service": svc}),
                    "follow_ups": ["Bound the in-memory cache (size limit / TTL)", "Alert on memory > 85%"]}
        if m.get("cpu_pct", 0) > 85 and m.get("rps", 0) > 2 * 120:
            return {"root_cause": f"Traffic spike ({m['rps']} rps, ~{m['rps'] // 120}x normal) saturating "
                                  f"{replicas} replicas of {svc}; autoscaler disabled.",
                    "severity": "SEV3", "query": "high latency cpu traffic spike scale",
                    "action": ("scale_service", {"service": svc, "replicas": min(20, replicas * 3)}),
                    "follow_ups": ["Re-enable the autoscaler", "Load-test before campaigns"]}
        return {"root_cause": f"{svc} is unhealthy but symptoms match no known pattern.", "severity": "SEV3",
                "query": "general triage", "action": None, "follow_ups": []}

    @staticmethod
    def _finish(root_cause: str, severity: str, affected: list, actions: list, follow_ups: list,
                confidence: float) -> ToolCall:
        return ToolCall("finish_triage", {"root_cause": root_cause, "severity": severity,
                                          "affected_services": affected, "actions_taken": actions,
                                          "follow_ups": follow_ups, "confidence": confidence})
