# Runbook: High error rate right after a deployment

Symptoms: 5xx / error rate spike, new exceptions in logs, deploy in the last 60 minutes.

1. Confirm the error spike started after the deploy (compare deploy time with the alert).
2. Check logs for new exception types that mention the new version.
3. If the new version is the likely cause: ROLL BACK to the previous version. Rollback is the fastest safe mitigation; fix forward later.
4. Verify error rate returns below 1% within 5 minutes after rollback.
5. Open a ticket for the owning team with the stack trace.
