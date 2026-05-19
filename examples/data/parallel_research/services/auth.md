# Authentication service

Acme authentication runs on signed JWT tokens. The auth-service emits
tokens with a 1-hour TTL via the `/auth/token` endpoint after verifying
the user's password against the bcrypt hash in the users table.

## Token rotation

Tokens auto-rotate on every authenticated request. The auth-service
returns a fresh token in the `X-Refresh-Token` response header; clients
should replace their cached token with that value.

## Rate limits

Authentication endpoints are rate-limited at 30 requests per minute per
IP. Exceeding this returns HTTP 429 with a `Retry-After` header.

## Key material

The signing key lives in the `auth-signing-key` Vault secret. It rotates
quarterly via the standard secret-rotation runbook.

## Common failures

- 401: token expired or invalid signature
- 403: token valid but user lacks the required permission
- 429: rate-limit exceeded
