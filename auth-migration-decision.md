# Decision: Migrate Authentication Service to Vendored OIDC Provider

**Status:** Proposed  
**Audience:** Engineering leadership  
**Decision needed by:** End of Q2 2026

---

## Summary

Approve migrating the authentication service from its current in-house implementation to a vendored OIDC provider. The Q3 2025 session-token leak showed that owning a hand-rolled token stack carries security risk we have already paid once. A vendored solution offloads token management, key rotation, and session handling to a provider whose core business is getting those right, while reducing long-term maintenance burden on the auth team. This document requests approval to begin vendor evaluation and contract negotiation; a separate review will confirm the specific vendor and cost before any contract is signed.

---

## Problem

The in-house authentication service has a structural maintenance burden and a proven security liability. In Q3 2025, a session-token leak from the legacy service triggered an incident response and a rebuild. The specific race condition that caused the leak was patched, but the underlying architecture was not changed: the same two-engineer auth team still owns a hand-rolled token lifecycle, session invalidation, and key rotation pipeline, with no dedicated security review cadence for the auth layer specifically. The rebuild fixed the known bug; it did not change the exposure surface.

Continued investment in this path means owning a problem that commercial OIDC providers have already solved at scale.

---

## Proposal

Replace the in-house authentication service with a vendored OIDC provider (Auth0, Okta, or AWS Cognito are the leading candidates). The migration covers:

- Token issuance and lifecycle (access, refresh, ID tokens)
- Session management and invalidation
- Key rotation and JWKS exposure
- MFA enforcement

Application code changes: services currently verifying tokens against the internal service switch to validating JWTs against the provider's JWKS endpoint (JWKS is a public key set published by the provider; services cache it and verify signatures locally, with no per-request external call). The interface to the rest of the stack is a standard OIDC token, so the change is largely contained to the auth layer. A pre-migration consumer audit will identify any services with coupling to internal token formats or custom claim structures that require additional work before cutover; a preliminary estimate is 2-4 services based on known internal integrations, confirmed by the audit.

---

## Alternatives Considered

**Continue on the rebuilt in-house service.**
The post-incident service is functional. The objection is not that it is currently broken, but that it requires ongoing security ownership of a custom token stack. Every new auth feature (SSO, device trust, step-up authentication) extends that surface. The Q3 2025 incident is evidence that this ownership has already produced a material failure once; there is no structural reason it cannot again.

**Rewrite the in-house service to be OIDC-compliant.**
This produces a standards-compliant result but leaves us maintaining the implementation. We would still own token issuance, key rotation, and session management; we would just do it in a spec-compliant way. OIDC compliance narrows the specification surface but does not provide independent security review, SOC 2 certification, or a bug bounty program. Engineering cost is comparable to the migration approach without the long-term maintenance savings.

**Self-hosted OIDC (e.g., Keycloak).**
Eliminates the external dependency and third-party data processing, which matters if compliance requirements (FedRAMP, data residency) rule out SaaS vendors. The tradeoff is that we own the deployment, upgrade cycle, and security patching of the OIDC server itself. This is the fallback if no SaaS vendor meets the latency benchmark.

**Vendored OIDC provider (proposed).**
Offloads token management, rotation, and session handling to a provider whose core business is getting these right. We inherit SOC 2 Type II certification, their incident response process, and their library maintenance. Engineering effort shifts from implementation to integration and configuration.

---

## Risks and Stakeholder Concerns

| Risk | Stakeholder | Likelihood | Mitigation |
|---|---|---|---|
| Latency regression at token validation | Platform | Medium | JWKS caching at the gateway (public keys change only on rotation; no per-request external call). Target: p99 under 5ms added, derived from the current 20ms auth budget and a 25% headroom constraint. Load test confirms before vendor selection. Fallback: self-hosted Keycloak if no SaaS candidate meets the benchmark. This mitigation depends on platform team bandwidth in early Q3 (open question 3). |
| Vendor lock-in | Platform | Low | Standard OIDC tokens mean application-layer code does not couple to a specific vendor. Switching vendors later requires re-configuration, not a code rewrite. |
| Token rotation policy gap | Security | Low | Vendor must support configurable token TTLs and rotation schedules matching current policy. All three candidates support this; confirmed during security review. |
| Migration divergence in production | All | Low | Shadow mode (dual-validate) for two weeks before cutover. Both systems validate tokens in parallel; any disagreement is logged and routed to an on-call alert rather than surfaced to users. The legacy service remains authoritative during shadow mode, so a divergence is an investigation item, not an outage. |
| Contract or compliance delay | Product / Legal | **High if required** | Whether a compliance review (FedRAMP, data-residency, internal procurement gate) is required and its lead time are unknown. This is the most schedule-sensitive open question: if a review takes 4-6 weeks, the Q3 cutover target slips. Needs resolution in week 1 after approval. |

**Security** is broadly supportive. Remaining ask: confirm the selected vendor's SOC 2 Type II report is current and supports custom claim mapping for internal roles.

**Product** wants a firm shipping date. The schedule below commits to full cutover by end of Q3 2026. New auth-dependent features (SSO, device trust) should be scoped against that date.

---

## Proposed Timeline

| Milestone | Target |
|---|---|
| Vendor selected, contracts signed | Week 2 of Q3 2026 |
| Internal staging environment integrated | Week 5 of Q3 2026 |
| Shadow mode (dual-validate in production) | Week 7 of Q3 2026 |
| Full cutover, legacy service decommissioned | End of Q3 2026 |

**Effort:** Approximately five person-weeks (two engineers across platform and auth teams) plus vendor procurement lead time.

**Cost estimate:** At approximately 500k MAU, SaaS vendor licensing ranges from roughly $500k-$1.5M/year, plus a support tier. Exact figures per vendor are included in the follow-on recommendation before any contract is signed. If no SaaS candidate clears the latency benchmark, the Keycloak fallback adds an estimated 3-4 person-weeks to deploy and harden, plus ongoing operational ownership from the platform team; that path would be re-scoped before proceeding.

**Dependencies:** The end-of-Q3 target assumes platform team bandwidth in early Q3 for the gateway caching work (open question 3) and no compliance review longer than two weeks (open question 2). Either constraint shifting moves all downstream milestones.

---

## Decision requested

Approve the migration from the in-house authentication service to a vendored OIDC provider, contingent on the vendor passing the latency benchmark and security review.

Approving this document authorizes the team to begin vendor evaluation and contract negotiation. It does not commit to a specific vendor or contract value; the vendor recommendation and exact cost come back to this same group for a separate sign-off before anything is signed.

If approved, the platform team will run the latency benchmarks and the security team will complete vendor review in the first two weeks of Q3 2026, with a migration target of end of Q3 2026.

**Sign-off table**

| Stakeholder | Role | Status |
|---|---|---|
| [Name] | Engineering leadership | Pending |
| [Name] | Security | Pending |
| [Name] | Platform lead | Pending |
| [Name] | Product | Pending |

---

## Open questions

1. Do we have a preferred vendor based on existing enterprise agreements (e.g., Okta if it's already in the IAM portfolio)?
2. Is there a legal or compliance review required before signing a new identity vendor contract, and what's the lead time?
3. Does the platform team have bandwidth in early Q3 for the gateway caching work, or does that need to be scoped separately?
