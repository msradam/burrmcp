# Deploy rollback runbook

## When to roll back

Roll back when:

- The canary triggered an automatic rollback (already done; verify).
- A deploy passed canary but production error rate is elevated.
- A deploy introduced a regression you can reproduce.

## How to roll back

1. Identify the bad release: `deploy-cli history <service>`.
2. Find the last known-good release id.
3. Run: `deploy-cli rollback <service> <release-id>`.
4. Wait for the rollback to propagate (about 2 minutes for full fleet).

## Verification

After rollback:

- Error rate should return to baseline within 5 minutes.
- p99 latency should return to baseline within 10 minutes.
- Spot-check one production host with `kubectl describe pod` to confirm
  it's running the rolled-back image.

If error rate stays elevated after 15 minutes, escalate to the service
owner.
