# Runbook: Memory leak / OOMKilled pods

Symptoms: memory near 100%, containers OOMKilled (exit code 137), restart count climbing.

1. Confirm OOMKilled events in logs and memory_pct above 90%.
2. Mitigation: RESTART the service to clear memory (buys time; it will come back if the leak remains).
3. Look for unbounded caches or growing collections in recent changes.
4. Follow-up: add a cache size limit / TTL, raise memory limit only as a temporary measure.
5. Verify memory drops and restarts stop.
