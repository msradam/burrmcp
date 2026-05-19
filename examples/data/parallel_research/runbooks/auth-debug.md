# Authentication debugging runbook

## Symptoms

- Spike in 401s from the auth-service
- User reports of "logged out" without action
- Failed login rate climbing in the auth dashboard

## First checks

1. Is the signing key intact? `vault kv get secret/auth-signing-key`
2. Is the users database reachable? `psql -h users-db -c "select 1"`
3. Is the rate limiter saturated? Check Grafana auth-ratelimit panel.

## Common causes

- Key rotation race: a new signing key deployed before all validators
  picked it up. Force a redeploy of validating services.
- Clock skew between the auth-service host and validators. The JWT
  `exp` check is strict.
- Database connection pool exhausted by a slow query.

## Recovery

Roll back to the prior auth-service release if the issue started after
a recent deploy. Run `deploy-cli rollback auth-service <release-id>`.
