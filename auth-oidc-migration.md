# Decision: Migrate Authentication Service to Vendored OIDC Provider

**Status:** Proposed  
**Audience:** Engineering leadership  
**Owner:** [Author]  
**Target decision date:** EOQ

---

## Summary

Replace the in-house authentication service with a vendored OIDC provider. The legacy service produced a session-token leak in Q3 2025. A vendored solution reduces the maintenance surface, provides a certified implementation of the OIDC spec, and eliminates the class of vulnerability that caused the incident.

---

## Background

The current authentication service is a bespoke implementation built over several years. In Q3 2025, a flaw in session-token issuance exposed active tokens to an unintended endpoint. The incident required an emergency rotation of all active sessions and a post-mortem that identified the root cause as insufficient validation in a custom token-signing path.

That incident accelerated a broader review. The review surfaced:

- No formal security audit of the token issuance and validation paths
- Divergence from current OAuth 2.0 / OIDC best practices in several flows
- Ongoing maintenance burden from owning the full implementation (key rotation, PKCE, device flow, token introspection)
- No automated compliance evidence for SOC 2 or similar audits

The security team reviewed vendored options and is largely satisfied with the leading candidates. The remaining open questions are latency impact (platform team concern) and delivery timeline (product team concern).

---

## Proposal

Adopt a managed OIDC provider (e.g., Auth0, Okta, or a self-hosted Keycloak deployment) as the system of record for authentication. The existing service becomes a thin adapter layer or is removed entirely, depending on vendor capability.

**Phase 1: Proof of concept (2 weeks)**  
Wire the vendored provider into a staging environment. Measure p50/p99 latency for token issuance and validation against current baseline. Confirm all existing OAuth flows (authorization code, device flow, machine-to-machine) are covered.

**Phase 2: Migration (4-6 weeks)**  
Migrate internal services to validate tokens against the vendor. Run old and new paths in parallel with feature flags. No forced user re-authentication until parallel validation is confirmed stable.

**Phase 3: Cutover (1 week)**  
Disable the legacy service. Decommission the custom token-signing infrastructure.

Total calendar time: 7-9 weeks from decision.

---

## Alternatives Considered

**Patch the existing service.** The Q3 incident was fixed, but the audit identified structural problems in the token-signing path. Patching the immediate issue leaves the broader maintenance and compliance burden in place. Not recommended.

**Rewrite in-house to current spec.** Estimated at 3-4 months for a production-ready implementation, longer for a full audit. Does not reduce the ongoing maintenance surface. Not recommended given the timeline.

**Self-hosted Keycloak.** Retains operational flexibility and keeps data on-prem. Adds infrastructure responsibility (upgrades, HA, key management). A reasonable choice if vendor data-sharing is a blocker; otherwise the managed options are lower friction.

---

## Risks and Trade-offs

**Latency.** An external OIDC provider adds a network hop for token validation. Platform's concern is valid. The Phase 1 POC should produce numbers. Token caching at the service level (standard practice for OIDC) typically keeps per-request overhead under 2 ms. If the POC shows otherwise, we revisit.

**Vendor dependency.** Authentication becomes a third-party dependency with its own SLA. Mitigated by: standard OIDC tokens remain valid locally if the vendor is unreachable for a short window; failover caching handles brief outages.

**Data residency.** Depending on which vendor is chosen, token metadata may leave the on-prem environment. Legal and security should sign off on the vendor's data processing agreement before Phase 2.

**Migration complexity.** Services that validate tokens directly (not through a shared library) need updating. A full audit of token consumers should run in parallel with Phase 1.

---

## Stakeholder Summary

| Team | Concern | Status |
|------|---------|--------|
| Security | Certified implementation, reduced custom attack surface | Largely satisfied; needs final vendor selection |
| Platform | Latency impact on auth-heavy paths | Open; resolved by Phase 1 POC |
| Product | Delivery timeline | 7-9 weeks from decision; spec by EOQ |

---

## Decision Requested

Approve proceeding to Phase 1 (proof of concept) with [preferred vendor or shortlist]. This unblocks the latency measurement that resolves the platform concern and puts the full migration on track to complete this quarter.

If approved, the technical spec with vendor selection rationale, token consumer audit, and rollback plan will follow within two weeks.
