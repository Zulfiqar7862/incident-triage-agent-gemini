"""Sources, done and not done: where each finding came from and what happened to each action.

Derived only from the run state that is already checkpointed (step records and the final
report), so it adds no new state and cannot affect routing or approvals.
"""
from __future__ import annotations

SOURCE_NAMES = {"list_services": "Service list", "get_metrics": "Metrics", "get_logs": "Logs",
                "get_recent_deployments": "Deployments", "search_runbooks": "Runbooks"}


def summarize(state: dict, report: dict | None) -> dict:
    sources, done, not_done = [], [], []
    for step in state.get("steps", []):
        for call in step["calls"]:
            if call.get("awaiting_approval"):
                continue
            if call.get("risky"):
                item = {"step": step["n"], "text": call["summary"] if call.get("ok") else call.get("error", "")}
                (done if call.get("ok") else not_done).append(item)
            elif call.get("ok") and call["tool"] in SOURCE_NAMES:
                sources.append({"step": step["n"], "source": SOURCE_NAMES[call["tool"]], "text": call["summary"]})
    if report and not done and not not_done:
        not_done.append({"step": None, "text": "No production change was proposed."})
    if report and report.get("routed_to_human"):
        not_done.append({"step": None, "text": f"Closing the incident: handed to a human "
                                                 f"({report['routing_reason']})."})
    return {"sources": sources, "done": done, "not_done": not_done}
