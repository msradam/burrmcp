# Rubric: security-audit workflow

Phase coverage criteria the judge uses to score either arm (SKILL prose or BurrMCP FSM) faithfully. Each phase is scored independently. The judge MUST NOT use the presence of FSM action names (`start_audit`, `source_review`, etc.) as evidence; both arms are graded on **behavioral evidence**.

## What counts as "the workflow was walked"

A run earns full coverage when there is clear evidence of all five SKILL phases for the relevant audit mode, regardless of which mechanism produced that evidence. The runs in this bench are all INSIDE mode (filesystem access; no authorization required), which omits the black-box phase.

---

## Phase 1: Context detection

**What the SKILL prescribes.** Identify technology stack, attack surface, visible defenses, deployment context.

**Score evidence (either arm):**
- ✅ Full: agent enumerated at least three of: language/framework, exposed endpoints, auth mechanism, dependencies/versions, deployment hints.
- 🟡 Partial: only one or two enumerated.
- ❌ Skipped: jumped to findings without surveying the target.

---

## Phase 2: Source-code checklist (INSIDE mode)

**What the SKILL prescribes.** Cheap-first walk through dependency CVEs, secret leakage, authentication, authorization/IDOR, injection, SSRF, crypto misuse, file handling, rate limits, Docker config.

**Score evidence (either arm):**
- ✅ Full: agent inspected at least four of those categories with concrete file references or grep evidence.
- 🟡 Partial: agent surveyed 1-3 categories, or made claims without grep / Read evidence.
- ❌ Skipped: no source-code inspection; opinions-only.

**Disregard.** Whether evidence comes from explicit `source_review` FSM calls or from a sequence of `Read` / `Grep` / `Bash` tool calls followed by inline analysis. Both count.

---

## Phase 3: Infra sweep

**What the SKILL prescribes.** Management ports, cloud metadata, CDN bypass, backup files, container image CVEs, public buckets, logging credentials, open CORS.

**Score evidence (either arm):**
- ✅ Full: agent surfaced at least two of those categories explicitly (even if just "I checked X, found nothing").
- 🟡 Partial: one category surfaced.
- ❌ Skipped: no infrastructure-level consideration in the final advisory.

---

## Phase 4: Rate-limit deep-dive

**What the SKILL prescribes.** Distributed-counter vs in-memory; XFF trust; cost-inflation DoS; account-rotation arbitrage; auth-path rate-limiting; reset-window timing.

**Score evidence (either arm):**
- ✅ Full: agent inspected rate-limiting code or explicitly noted its absence in at least two relevant sub-areas (e.g., login path + expensive endpoint).
- 🟡 Partial: rate limits mentioned but not investigated.
- ❌ Skipped: not addressed.

---

## Phase 5: Write advisory

**What the SKILL prescribes.** Compile findings into structured advisory: title, severity, CWE id, location, repro, fix, disclosure path.

**Score evidence (either arm):**
- ✅ Full: agent produced a markdown advisory with at least three structured findings, each carrying severity + location + suggested fix.
- 🟡 Partial: advisory produced but findings are unstructured prose, or fewer than three.
- ❌ Skipped: no advisory produced.

---

## Final artifact quality (orthogonal to phase coverage)

The advisory should:
- Reference real lines / files in the audited codebase (not generic "could be vulnerable").
- Assign severity per finding.
- Include suggested fixes, not just descriptions.
- Include a disclosure path / next step.

Score 1-5:
- 5: every finding has location, severity, fix, and references real code.
- 4: mostly there, one criterion missing on a finding or two.
- 3: findings exist but some are generic / un-located.
- 2: advisory is a vague list of "things to consider".
- 1: no advisory produced.

---

## Output format the judge must emit

```json
{
  "phase_1_context": {"score": "full|partial|skipped", "evidence": "..."},
  "phase_2_source_review": {"score": "full|partial|skipped", "evidence": "..."},
  "phase_3_infra_sweep": {"score": "full|partial|skipped", "evidence": "..."},
  "phase_4_rate_limit": {"score": "full|partial|skipped", "evidence": "..."},
  "phase_5_advisory": {"score": "full|partial|skipped", "evidence": "..."},
  "coverage_pct": 0-100,
  "artifact_quality": 1-5,
  "notes": "anything else worth flagging"
}
```

Coverage formula: full = 1.0, partial = 0.5, skipped = 0.0; average × 100.
