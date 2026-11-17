# Runbook: General incident triage

1. List services and find which are unhealthy.
2. For each unhealthy service gather metrics, logs and recent deployments.
3. Form a hypothesis, find the matching runbook, apply the smallest safe mitigation.
4. Every change to production needs human approval.
5. If nothing is unhealthy, do not invent a fix: report "no issue found" and hand back to a human.
