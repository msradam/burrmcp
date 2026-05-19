# Incident response runbook

Severity levels:

- P0: complete outage affecting all users.
- P1: degraded service affecting more than 10% of traffic.
- P2: partial degradation or non-critical service down.
- P3: minor issue, fixable in business hours.

## When paged

1. Acknowledge in PagerDuty within 5 minutes.
2. Open an incident channel: `#inc-<short-name>`.
3. Post initial summary in the channel and to status page if P0 or P1.
4. Page secondary on-call if you need help triaging.

## Communications

P0 and P1 incidents get a status page update every 30 minutes until
mitigated. Update via the `statuspage` CLI or the admin web UI.

## Postmortem

P0, P1, and any P2 lasting more than 4 hours require a written
postmortem within 5 business days.
