# Rubric: doc-coauthoring workflow

Phase coverage criteria the judge uses to score either arm (SKILL prose or Theodosia FSM) faithfully. Each phase is scored independently. The judge MUST NOT use the presence of FSM action names (`start_doc`, `gather_context`, etc.) as evidence; both arms are graded on **behavioral evidence**, not jargon.

## What counts as "the workflow was walked"

A run earns full coverage when there is clear evidence of all three SKILL stages, regardless of which mechanism (prose-followed or FSM-enforced) produced that evidence.

---

## Stage 1: Context Gathering

**What the SKILL prescribes.** Close the gap between agent and human knowledge before drafting. Two sub-phases:
- **1a. Info dump.** The agent gathers (or surfaces what it can infer of) project background, stakeholders, constraints, prior incidents, timeline, technical context.
- **1b. Clarifying questions.** The agent surfaces gaps; either asks the human, or self-answers with flagged assumptions.

**Score evidence (either arm):**
- ✅ Full: the trace shows the agent enumerated context across at least three of: background, stakeholders, constraints, timeline, technical detail, prior incidents. AND surfaced at least one clarification or assumption explicitly.
- 🟡 Partial: the trace surfaces some context (≥1 category) but skips clarifications, OR surfaces clarifications without context enumeration.
- ❌ Skipped: the trace jumps directly to writing the doc without enumerating context or surfacing clarifications.

**Disregard.** Whether the evidence comes from explicit `gather_context` / `confirm_context` FSM calls or from in-chat text under headings like "Stage 1" / "Context Gathering" / "Background". Both count equally.

---

## Stage 2: Refinement and Structure

**What the SKILL prescribes.** Two sub-phases:
- **2a. Agree structure.** Propose a section list before drafting.
- **2b. Per-section drafting.** Each section gets brainstorm → curation → draft.

**Score evidence (either arm):**
- ✅ Full: the trace shows an explicit section list proposed BEFORE drafting begins, AND each section has draft content produced (markdown body, not just a heading).
- 🟡 Partial: drafting happens but without a pre-agreed section list, OR a structure is proposed but not every listed section gets drafted.
- ❌ Skipped: no doc was produced.

**Disregard.** Whether the structure was agreed via an explicit `agree_structure` FSM call or by the agent saying "Here's the structure: ... Now I'll draft section A..." in chat.

**Disregard.** Whether the doc lives in a file (Write/Edit tool calls) or inline in the final assistant text. Both are valid artifacts as long as the content exists.

---

## Stage 3: Reader Testing

**What the SKILL prescribes.** Test the doc with a fresh perspective (sub-agent, predicted questions, simulated cold read) to catch blind spots before the human reader does. Output: identified issues.

**Score evidence (either arm):**
- ✅ Full: the trace shows at least one of: (a) explicit invocation of a sub-agent / fresh-context reader, (b) the agent enumerated predicted reader questions and answered them from doc-only context, (c) the agent listed specific ambiguity / contradiction / assumption issues in the doc.
- 🟡 Partial: the agent mentioned the need for reader testing or listed generic risks but did not actually test.
- ❌ Skipped: no reader-testing artifact of any kind.

**Disregard.** Whether the test came from an FSM `reader_test` call or from a `Agent` / sub-agent tool invocation or from inline simulated questions in chat. All three count.

---

## Final artifact quality (orthogonal to phase coverage)

Even when all phases are walked, the final doc itself should:
- Address the audience and desired impact stated in the prompt.
- Cite the past incident (Q3 2025 session-token leak) as motivation.
- Surface at least two of the three stakeholder perspectives (security, platform, product).
- Propose a concrete migration path or timeline, not just a recommendation.

Score 1-5:
- 5: hits every criterion above with specifics.
- 4: hits criteria with light handwaving on one.
- 3: hits the recommendation but missing a stakeholder voice or the incident reference.
- 2: vague recommendation, no incident context, no stakeholder voices.
- 1: no usable doc produced.

---

## Output format the judge must emit

```json
{
  "stage_1_context_gathering": {"score": "full|partial|skipped", "evidence": "..."},
  "stage_2_refinement_structure": {"score": "full|partial|skipped", "evidence": "..."},
  "stage_3_reader_testing": {"score": "full|partial|skipped", "evidence": "..."},
  "coverage_pct": 0-100,
  "artifact_quality": 1-5,
  "notes": "anything else worth flagging"
}
```

Coverage formula: full = 1.0, partial = 0.5, skipped = 0.0; average × 100.
