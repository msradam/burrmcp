# Decision Doc: Migrate Authentication Service to Vendored OIDC Provider

**Status:** Draft  
**Author:** [Your name]  
**Date:** 2026-05-24  
**Decision owner:** VP Engineering  
**Target decision by:** End of Q2 2026

---

## Summary

The Q3 2025 session-token leak exposed a structural problem: the company's
in-house authentication service carries security and compliance risk that
grows as the product scales, and is not a differentiator worth owning. This doc
proposes replacing it with Auth0, a SOC 2-attested OIDC provider. Token
validation stays local (no latency regression), existing sessions migrate
transparently, and the rollout is phased with a feature-flag rollback available
until decommission. Security has reviewed and is satisfied; platform's latency
concern is addressed by local JWT validation; product gets an 11-week delivery
commitment from decision approval. We are requesting sign-off to proceed.

---

## Problem & Background

The company's authentication service is a bespoke implementation that has grown
incrementally over several years without a dedicated owning team. In Q3 2025 a
session-token leak in the legacy service exposed active user sessions. The
incident required an emergency credential rotation, customer notification, and a
multi-week engineering response. A post-mortem identified the root cause as an
insecure token-generation algorithm that had not been updated since initial
implementation.

The incident accelerated an existing concern: building and operating a
production-grade auth service is not a product differentiator. Each engineering
hour spent patching auth libraries, rotating signing keys, and debugging session
edge cases is an hour not spent on customer-facing features. Ownership of the
service is diffuse across the platform team, with no engineer holding full
context on the implementation.

The in-house service also carries compliance risk. It lacks third-party SOC 2
attestation and does not support modern standards (FIDO2/WebAuthn) that
enterprise customers are beginning to require in procurement questionnaires.

---

## Proposed Solution

Adopt **Auth0** as the company's identity platform, replacing the in-house
token issuance and validation layer.

Auth0 is an OIDC/OAuth 2.0-compliant identity provider with a SOC 2 Type II
attestation, built-in MFA and FIDO2/WebAuthn support, and a managed key
rotation lifecycle. The migration surface is narrow: the internal `AuthClient`
abstraction is the sole integration point. Callers of that interface would see
no API changes; only the backing implementation changes.

Token validation moves to standard JWT verification using Auth0's public JWKS
endpoint. To avoid a synchronous external call on every request, tokens are
validated locally using the cached public key material, falling back to the JWKS
endpoint only on key-set changes. This is the same pattern used by all major
OIDC implementations. The cryptographic verification step itself is
sub-millisecond; end-to-end P99 validation latency (including middleware and
request parsing) measured 4 ms in load testing, well within platform's 50 ms
budget.

Auth0 tenants will be provisioned per environment (dev, staging, prod) with
production isolation enforced at the tenant boundary. Existing user sessions
will be migrated transparently; end users will not see a login prompt as a
result of the migration.

Licensing falls under the security remediation budget approved following the
Q3 2025 incident. The specific contract value is in the procurement brief
attached to this doc; no new budget line is required.

---

## Alternatives Considered

**Harden the in-house service.** A dedicated auth team could be formed and the
service rewritten to address the Q3 root cause. This was rejected because it
defers the underlying problem (owning auth is a cost center) rather than
eliminating it. A rewrite would also not provide SOC 2 attestation or the
built-in MFA/FIDO2 surface that enterprise customers require.

**Self-hosted Keycloak.** Keycloak is a mature open-source IdP that would
address compliance and standards gaps without a vendor dependency. It was
rejected because it reintroduces operational overhead (infrastructure
provisioning, version upgrades, CVE response) that is similar in character to
the current problem. It also provides no SOC 2 attestation.

**AWS Cognito.** Cognito was evaluated as the lowest-friction option for teams
already on AWS. It was rejected due to feature gaps in enterprise SSO flows and
a weaker OIDC standards story compared to Auth0, which would require custom
workarounds for compliance questionnaire responses.

**Okta.** Okta offers equivalent capabilities at a higher per-seat price with a
more complex enterprise sales cycle. Suitable as a future migration target if
company scale warrants it; not the right fit for current stage.

---

## Stakeholder Concerns & Mitigations

**Security: vendor lock-in and key material custody.**
Signing keys are managed by Auth0 under their SOC 2-attested key management
process. We do not hold private key material, which reduces our blast radius in
a credential compromise scenario. OIDC is an open standard, so migration to a
different provider in the future requires only a tenant-level configuration
change. Security has completed its strategic review of Auth0 (DPA, compliance
posture, key custody) and has no blocking concerns. Per-phase technical
sign-offs (token rotation config, load test results) remain as rollout gates
and are tracked in the migration runbook.

**Security: existing session continuity during migration.**
Active sessions from the legacy service cannot be re-issued as Auth0 JWTs
without a re-authentication event. The plan is a short (72-hour) token TTL
reduction two weeks before cutover, so that the majority of active tokens expire
naturally before migration. Remaining sessions trigger a silent re-auth via
refresh token. No user-visible logout event is expected for more than ~2% of
active sessions.

**Platform: token validation latency.**
Token validation uses cached public key material from Auth0's JWKS endpoint.
After warm-up, validation is a local cryptographic operation with no network
dependency. The external call occurs only on key rotation, which Auth0 performs
on a low-frequency schedule. Load testing against the Auth0-backed service
showed P99 validation at 4 ms, within platform's 50 ms budget.

**Platform: Auth0 availability dependency.**
Token validation continues during an Auth0 outage as long as public keys are
cached. Only new sign-in flows require Auth0 reachability. Auth0's SLA is
99.99% uptime with a published status page. The runbook for an extended outage is: set the `AUTH_TOKEN_TTL_OVERRIDE`
config flag (an existing platform control) to extend cached token lifetimes
by up to 24 hours, and alert on-call. New sign-in attempts during an outage
will fail until Auth0 recovers; this is acceptable given the 99.99% SLA.

**Product: committed timeline.**
The rollout is 11 weeks from decision approval. See the table below. A signed
vendor agreement and environment provisioning complete in the first two weeks,
putting integration work on track for a Q3 production cutover.

---

## Rollout Plan

Migration is phased by service, with each phase gated on load test results and
security sign-off.

| Week | Milestone |
|------|-----------|
| W+1 | Decision approved; vendor agreement signed |
| W+2 | Auth0 tenants provisioned (dev, staging, prod) |
| W+3–4 | Internal tooling (CI, admin dashboard) migrated; low blast radius if issues surface |
| W+5–6 | Core API services migrated; load tested at 2x peak; P99 validated |
| W+7 | Token TTL reduction for legacy sessions; silent re-auth path exercised |
| W+8 | Production cutover for user-facing services |
| W+9–10 | Legacy auth service in read-only mode (validates existing tokens, no new issuance); monitoring period |
| W+11 | Legacy service decommissioned |

Total duration: 11 weeks from decision. A Q2 EOQ decision puts decommission at
week 11 of Q3, leaving roughly one week of buffer before Q3 close.

A feature flag controls the auth backend per service. Rollback to the legacy
service is a single flag flip with no data migration required, available until
Week 11. After decommission, rollback is not available; recovery from a
post-decommission issue would require forcing a re-login for affected users,
with no data loss risk.

---

## Decision Requested

Approve the migration from the in-house authentication service to Auth0 as the
company's OIDC identity provider, and authorize:

1. Signing the Auth0 vendor agreement under the existing security remediation
   budget.
2. Allocating platform team capacity for an 11-week migration (approximately
   1.5 engineers).
3. Designating a migration directly responsible individual (DRI) from the
   platform team with cross-functional coordination authority over security
   and product timelines.

A decision by [EOQ date] keeps the rollout on track to complete before the
close of Q3.
