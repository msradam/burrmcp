# Decision Doc: Migrate Authentication Service to Vendored OIDC Provider

**Status**: Draft  
**Author**: [Author]  
**Date**: 2026-05-24  
**Approvers**: Engineering Leadership  
**Informed**: Security (configuration review gate in Phase 2), Platform (latency benchmark gate in Phase 3), Product (timeline)

---

## Background

The company has operated a homegrown OAuth2 authorization server since before auditable
identity infrastructure was a SOC2 requirement. At the time, no vendor met the security
and data-residency bar, so the in-house service was a reasonable choice.

In Q3 2025, a logic error in the token-issuance layer allowed session tokens to be
minted without enforcing expiry, exposing active user sessions across three products
for approximately 18 hours before detection. The incident required a partial rebuild
of the token-issuance layer and a coordinated credential rotation affecting all active
sessions. Full incident report is available in the internal postmortem. Post-incident,
the service is stable, but the underlying architecture carries significant technical
debt: custom JWT libraries with no upstream security patches, no native MFA flows, and
token introspection that runs as a synchronous database query.

Since then, OIDC-compliant vendors (Okta, Auth0, Cognito) have matured to meet our
compliance requirements. Maintaining a custom authorization server is not core
engineering work, and the Q3 incident surfaced how costly that maintenance gap can be.

---

## Problem Statement

The in-house auth service has four compounding problems that make continued investment
a poor tradeoff:

- **Security posture**: The service uses custom JWT libraries with no upstream patch
  channel. When CVEs appear in the underlying crypto primitives, we respond ad hoc.
  The Q3 2025 token leak is the most recent consequence.

- **MFA coverage**: Multi-factor auth is a bolt-on feature, not a native flow. Enforcing
  it consistently across products requires per-product engineering work and has led to
  uneven coverage.

- **SOC2 audit burden**: External auditors flag the custom implementation as a scope
  risk every cycle. Remediating their findings consumes security team capacity that
  would otherwise go toward product-facing work.

- **Feature velocity**: SSO federation requests (new enterprise customers, internal
  tools) require months of eng time per integration. A vendor handles this at the
  protocol layer.

The Q3 incident bought us a window to address the root cause rather than re-patch.
That window closes if we don't act this quarter.

---

## Proposed Solution

Migrate the authorization server to **Okta** (OIDC-compliant, SOC2 Type II certified,
FedRAMP Moderate authorized) and decommission the in-house service within two quarters.

**Auth flows:**

- Browser clients: Authorization Code + PKCE. No implicit flow.
- Service-to-service: Client Credentials. Scoped per service pair.

**How this addresses each problem:**

| Problem | Resolution |
|---|---|
| Custom JWT libs / CVE exposure | Okta owns the crypto stack and patches upstream |
| Inconsistent MFA | MFA policy enforced at the provider; products inherit it |
| SOC2 audit risk | Okta's SOC2 report satisfies scope; auditors accept vendor certs |
| SSO federation latency | Okta's SAML/OIDC federation layer; no per-integration eng work |

**Integration approach:**

Products integrate via a thin adapter library that wraps Okta's SDK. This decouples
product code from the vendor SDK surface and makes a future vendor swap a single-library
change rather than a multi-product migration.

Token validation runs as local JWT signature verification against Okta's JWKS endpoint,
eliminating the synchronous database query on the introspection path.

---

## Alternatives Considered

**Rebuild on Ory Hydra (self-hosted OSS)**  
Hydra is a well-maintained OIDC server that would eliminate the custom-JWT problem.
Ruled out: it preserves the operational burden (patching, scaling, incident response)
that drove the Q3 leak in the first place. SOC2 scope risk remains.

**Auth0**  
Technically equivalent to Okta for this use case. Ruled out: we have an existing Okta
enterprise contract (currently used for workforce SSO) that covers the Customer Identity
use case under the same agreement. Expanding scope requires a contract amendment, not a
new procurement cycle. Auth0 would require a full new vendor procurement cycle, delaying
the timeline past EOQ.

**Keycloak (self-hosted)**  
Feature-complete OIDC provider, actively maintained. Ruled out: self-hosted means we
own availability and patching. Same class of risk as the status quo, with a migration
cost on top.

**Continue patching the in-house service**  
Viable in the short term. Ruled out: the Q3 incident demonstrated that the patching
cadence is reactive, not proactive. SOC2 auditors have flagged this three cycles
running, and the next finding could affect our certification status.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Session disruption during cutover | Medium | Parallel-run period: Okta issues new tokens while in-house service validates existing ones. Force-expire in-house sessions at cutover T+2 weeks. |
| Latency increase on token validation (during rollout) | Medium | Steady-state is faster than the current synchronous DB query once JWKS caching is in place. The risk window is during Phase 1-2 before caching is tuned. Platform team estimate: p99 delta <5ms vs. baseline once caching is live. Phase 3 gate on measured benchmarks before full rollout. |
| Vendor lock-in | Low | Adapter library abstracts the Okta SDK. Migration to another OIDC provider is a library change, not a product change. |
| Okta availability incident | Low | Okta's SLA is 99.99%. In-house service SLA is uncontracted. Fallback: short-lived token cache (5-minute TTL) keeps read-only flows live during brief outages. |
| Migration stalls; two systems in parallel indefinitely | High (without a hard deadline) | Decommission date is a hard dependency in the project plan. In-house service has a calendar end-of-life set at T+6 months from cutover start. |

---

## Implementation Timeline

| Phase | Work | Target |
|---|---|---|
| **Phase 1** | Okta tenant provisioning, adapter library, dev environment integration | Q3 2026 (6 weeks) |
| **Phase 2** | Pilot migration: lowest-traffic internal product. Security sign-off gate. | Q3 2026 (4 weeks) |
| **Phase 3** | Migrate remaining consumer-facing products. Enterprise SSO federations migrated to Okta. | Q4 2026 (8 weeks) |
| **Phase 4** | Parallel run: Okta issues tokens, in-house service accepts existing sessions. | Q4 2026 (2 weeks) |
| **Phase 5** | Force-expire legacy sessions. In-house service receives no new auth requests. Decommission begins. | Q1 2027 |

**Gates:**
- Phase 2 requires security team sign-off on Okta configuration review.
- Phase 3 requires latency benchmarks from platform (p50/p99 against baseline).

The spec for Phase 1 is due this quarter. Approval of this doc unblocks that work.

---

## Cost

| Item | Estimate |
|---|---|
| Okta Customer Identity contract amendment | ~$X/year (contract negotiation in progress; Finance has the draft) |
| Engineering (Phase 1-2): 1 senior + 2 engineers, ~10 weeks | ~$Y loaded (use team's standard loaded rate) |
| Engineering (Phase 3-5): same team, ~12 weeks | ~$Z loaded |
| Infrastructure delta (retiring in-house auth infra) | Net reduction; exact figure in Phase 1 spec |

Licensing costs are tracked separately in the Okta contract negotiation. This doc
requests headcount allocation; budget approval follows the standard procurement path.

---

## Decision Requested

Engineering leadership approval is needed on three items:

1. **Direction**: Approve Okta as the target OIDC provider and authorize the migration
   project to begin.

2. **Resourcing**: Allocate one senior engineer (tech lead) and two engineers to
   Phase 1 and Phase 2 starting Q3 2026.

3. **Decommission commitment**: Treat the in-house auth service end-of-life date (Q1 2027)
   as a hard engineering deadline, not a target.

If approved, the Phase 1 technical spec will be published within two weeks and Phase 1
work begins immediately after.
