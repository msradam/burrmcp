# Secrets FAQ

## Where are secrets stored?

All secrets live in HashiCorp Vault at `secret/<service>/<key>`.
Services read them at startup via the standard Vault sidecar.

## How do I rotate a secret?

1. Generate the new secret value.
2. Write it to Vault: `vault kv put secret/<service>/<key> value=<new>`.
3. Trigger a rolling redeploy of services that consume it.
4. After redeploy, revoke the old credential at the source (e.g., the
   Stripe dashboard for `stripe-webhook-secret`).

## What's the rotation schedule?

- Auth signing keys: quarterly.
- Database passwords: every 6 months.
- Third-party API keys: per provider's recommendation, at least
  annually.

## How do I add a new secret?

Write it under `secret/<service>/<key>` in the appropriate Vault
namespace. Update the consuming service's Vault policy to allow read on
that path. Add the path to the secrets-inventory document so it gets
included in the next rotation review.
