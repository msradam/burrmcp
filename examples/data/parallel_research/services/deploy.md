# Deploy pipeline

Acme's deploy pipeline is GitHub Actions plus the internal `deploy-cli`
tool. Every merge to main triggers a canary deploy to one host in the
production fleet.

## Canary stage

The canary runs for at least 30 minutes before promotion. The
canary-monitor watches error rate, latency p99, and saturation; any
metric out of bounds blocks promotion.

## Promotion

After the canary clock expires and metrics look healthy, the deploy
promotes to the rest of the fleet in batches of 10% with 5-minute
soaks between batches.

## Rollback

A failed canary triggers automatic rollback. Manual rollback is
`deploy-cli rollback <release-id>` and takes about 2 minutes for the
full fleet. See the deploy-rollback runbook for the verification steps
after a rollback.
