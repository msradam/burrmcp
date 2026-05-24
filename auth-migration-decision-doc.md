# Decision Doc: Migrate Authentication Service to Managed OIDC Provider

**Status:** Proposed  
**Author:** [Author]  
**Reviewers:** Engineering Leadership, Security, Platform, Product  
**Target decision date:** Q2 2026 EOQ

> **Note on vendor naming:** This doc refers to Auth0 as the proposed provider. Vendor selection is pending final contract review; no contract has been signed.

---

## Summary

We propose replacing the in-house authentication service with a managed OIDC provider, Auth0. The legacy service was the root cause of the Q3 2025 session-token leak. A partial rebuild is underway but is still carrying the structural debt that made the incident possible. Outsourcing token issuance, rotation, MFA, and session management to a certified provider eliminates that debt, reduces ongoing engineering ownership cost, and reaches the same GA date as completing the rebuild. Ongoing risk is lower; custom auth code is gone. Local JWT verification keeps validation latency within our existing SLA. We are requesting approval to finalize vendor selection and begin the integration RFC.

---

## Background

In Q3 2025, a session-token leak from the authentication service exposed user sessions across multiple services platform-wide. Root cause: the in-house token issuance layer had no automatic rotation, weak expiry enforcement, and insufficient audit logging. The blast radius required emergency token invalidation across the platform.

A rebuild effort started after the incident. That rebuild is not yet GA and still inherits the original service's data model and deployment footprint. The team has addressed the most acute issues (expiry enforcement, basic audit logging) but three structural gaps remain unresolved: single-key token issuance with no JWKS rotation boundary, no automatic session revocation on logout across services, and no MFA support. Each requires a data model change and coordinated service rollout.

Authentication is a shared dependency for every user-facing and internal service. Engineering effort spent maintaining a custom implementation is effort not spent on product work. The more complex the auth service grows, the higher the risk that the next incident comes from a gap in our own implementation rather than a known, externally-patched vulnerability.

The rebuild is on hold pending this decision: the team has not invested further in the rebuild path while the managed-migration option is under evaluation, because closing those three gaps would require a scope of work roughly equivalent to the integration effort for a managed migration. Both paths project GA in Q4 2026 under current estimates. The managed migration eliminates all three gaps on day one and transfers ongoing security ownership to the provider; the rebuild eliminates none of them until custom work is complete, and we retain ownership permanently afterward.

---

## Proposal

Replace the in-house authentication service with Auth0, a managed OIDC provider.

**Scope:**
- All user-facing authentication (login, session management, MFA, token refresh)
- Federated identity: bridge to the existing corporate IdP via OIDC/SAML
- Out of scope: machine-to-machine authentication (service accounts, CI credentials); those continue using the client credentials flow and are not affected by this migration

**How it works:**
- Auth0 issues and rotates tokens. Our services validate JWTs locally using public keys fetched from the provider's JWKS endpoint and cached with a 60-second refresh interval. Token validation does not require a network call on the critical path; it is a local HMAC/RSA verify against a cached key. On cache miss or key rotation, one background refresh call fetches the new JWKS; in-flight requests are not blocked and use the previously cached key until the refresh completes. Formal latency benchmarking against our P99 SLA (currently ~20ms for auth validation) is a Q3 staging deliverable; GA cutover is gated on those benchmarks passing. We are requesting approval now because the Q3 integration work must begin in early Q3 to hold the Q4 GA date; waiting for benchmark results before approving would push GA into H1 2027. If Q3 benchmarks show that Auth0's P99 latency exceeds our 20ms target and cannot be brought within SLA through configuration (JWKS cache tuning, regional endpoint selection), the team will surface that result to leadership before GA cutover and the migration may be paused or redirected to a self-hosted option (see Alternatives).
- MFA, session policy, and audit logging are owned by the provider. We configure policy; we do not implement it.
- The corporate IdP federation (employee SSO) is handled via Auth0's SAML/OIDC bridge, preserving the current login experience.

**Migration approach:**
Run the legacy service and Auth0 in parallel during the integration phase. Route new sessions through Auth0 while legacy sessions expire naturally. Cutover is traffic-shaped, not a hard flag-flip.

---

## Stakeholder Positions

**Security:** Reviewed the proposal and the risk table. Approves the migration direction. Remaining conditions (not blockers on this approval): the integration RFC must include a threat model for the parallel-run period and define token invalidation behavior at rollback.

**Platform:** Conditional approval. Platform accepts the JWT local-verification design and will formally sign off on the migration at the start of Q3, after a one-week review of the integration RFC. GA cutover approval is separate and requires passing P99 latency benchmarks in Q3 staging.

**Product:** No objection. Accepts Q4 2026 as the GA target and will confirm resourcing once this doc is approved.

**Compliance:** Reviewed Auth0's SOC 2 Type II certification and data residency options. Not blocking.

---

## Alternatives Considered

**Complete the in-house rebuild**

Continuing the rebuild reaches GA in roughly the same timeframe as a managed migration, with higher ongoing ownership cost and no external security review. The service would still be a custom implementation that our team is responsible for auditing, patching, and operating. This path does not meaningfully reduce the risk profile that produced the Q3 incident.

**Self-hosted Keycloak**

Keycloak is a mature open-source OIDC server. It removes custom token logic and gives us a well-audited codebase. However, it requires us to own the deployment, upgrades, and availability of the auth service. We retain operational risk without gaining the SLA or compliance certifications of a managed offering. Recommended only if data residency requirements preclude a SaaS provider.

**Partial migration (outsource MFA only)**

Delegating only MFA to an external provider while keeping session logic in-house splits the auth surface between two systems without eliminating the vulnerable component. This adds integration complexity without closing the gap.

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Validation latency increase | Low | Medium | JWT local verification; JWKS keys cached with a short TTL. No per-request external call. |
| Vendor lock-in | Medium | Medium | OIDC is an open standard. Migrating between compliant providers is painful but straightforward. We are not building on proprietary APIs. |
| Cost at scale | Low | Low | SaaS pricing at scale is covered in the vendor evaluation. Projected cost is less than current engineering headcount cost for maintaining the service. |
| Data residency / compliance | Low | High | Auth0 offers SOC 2 Type II certification and regional data tenancy options. Compliance team has reviewed and is not blocked. |
| Dual-system migration risk | Medium | Medium | Parallel-run period is bounded (target: 6 weeks). Legacy sessions expire; no forced logout required. Rollback path during parallel-run: stop routing new sessions to Auth0 and drain back to legacy. Post-decommission rollback is not available; the integration RFC must define the point-of-no-return criteria before the legacy service is retired. |
| Feature parity gaps | Low | Medium | Gap analysis is part of the integration RFC. Known bespoke behaviors (custom session metadata fields) are documented and have vendor equivalents. |
| Incident response dependency on vendor SLA | Medium | Medium | Auth0 SLA: 99.99% uptime, 1-hour critical-incident response. That is better than our current on-call coverage for the auth service. Degraded-mode behavior (cached tokens remain valid for up to 30 minutes after JWKS refresh failure) is defined. |

---

## Implementation Timeline

| Phase | Quarter | Deliverables |
|---|---|---|
| Decision + Vendor | Q2 2026 (now) | This doc approved; vendor contract signed; integration RFC drafted |
| Integration + Staging | Q3 2026 | Auth0 wired in staging; parallel-run begins; load and latency benchmarked |
| GA Cutover | Q4 2026 | New sessions routed through Auth0 for all user-facing services; monitoring in place |
| Legacy Decommission | Q1 2027 | In-house auth service retired; rebuild branch closed |

The integration RFC (Q3 deliverable) will detail the service-by-service cutover order, rollback criteria, and monitoring plan.

---

## Decision Requested

Two approvals are requested:

1. **Migration direction:** Approve replacing the in-house auth service with a managed OIDC provider (Auth0 pending contract). This authorizes the team to finalize the vendor contract and begin the integration RFC.
2. **GA cutover** is a separate decision, gated on Q3 latency benchmarks meeting the 20ms P99 SLA, and requires no action from leadership at this stage.

A no-decision on item 1 by Q2 2026 EOQ defaults to resuming the in-house rebuild, which carries higher ongoing risk and does not close the Q3 incident's root cause within the same timeframe.
