# Runbook: High latency with CPU saturation

Symptoms: p99 latency high, CPU above 85%, request queue growing, traffic (rps) above normal.

1. Compare rps with the normal baseline (~120 rps). A traffic spike with saturated CPU means capacity problem, not a code bug.
2. Mitigation: SCALE OUT the service (add replicas, about 3x current for a 3x spike).
3. Re-enable the autoscaler after the incident.
4. Verify CPU below 70% and p99 latency below 500 ms.
