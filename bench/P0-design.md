# P0: SKILL.md vs Theodosia head-to-head

The experiment that decides whether Theodosia ships under the enterprise / capability-amplifier framing or under the research-curiosity framing. Goal: one decisive number per workflow per model on whether FSM-enforced gates beat prose-instructed gates on phase ordering and reproducibility.

## What we are comparing

Two conditions, same workflow, same agent, same target.

| Condition | Mechanism | Phase enforcement |
|---|---|---|
| **SKILL** | Agent reads `examples/skills/<skill>/SKILL.md` prose, uses raw `Read` / `Grep` / `Bash` / `Write` tools | Prose tells the agent the phase order. Agent decides whether to honor it. |
| **Theodosia** | Agent calls `step(action, inputs)` on the mounted FSM. Each action emits a prompt; agent runs that phase using its raw tools; reports findings back via the next `step`. | FSM refuses out-of-order `step` calls with `invalid_transition` + `valid_next_actions`. |

Identical work content in both conditions: the agent's underlying tools are the same, the targets are the same, the phase definitions are the same. Only difference: who enforces the order.

## Workflows (already converted, ready to run)

| Skill | FSM demo | Phases |
|---|---|---|
| `security-audit` | `examples/security_audit.py` | start_audit → record_context → source_review → blackbox_review (cond) → infra_sweep → rate_limit_deep_dive → write_advisory |
| `differential-review` | `examples/differential_review.py` | 7 phases incl. HIGH-risk gate forcing adversarial + deep-context |
| `fp-check` | `examples/fp_check.py` | 8 phases incl. 6 mandatory gate reviews before TRUE/FALSE verdict |
| `webapp-testing` | `examples/webapp_testing.py` | 7 phases incl. reconnaissance gated behind `networkidle` |

Targets: Flask checkout (already in `tests/smoke/conftest.py`) for security-audit and webapp-testing; synthetic PR diffs (to be shipped in `bench/fixtures/`) for differential-review and fp-check.

## Models

| Tier | Model | Why |
|---|---|---|
| Frontier ceiling | Claude Opus 4.7 | smoke-test target; the demo passes here |
| Frontier vendor cheap | Claude Haiku 4.5 | where the FSM should start helping |
| Cross-vendor sanity | GPT-5 | nobody can say the result is Claude-rigged |
| Open / small | Llama-3.x-8B or Granite-8B | the capability-amplifier story lives or dies here |

Skip Sonnet, Gemini, Granite-large unless time permits. The matrix above is the cheapest matrix that supports the four framings.

## Prompt tracks per workflow

Run each (model × workflow × condition) cell on three input tracks:

1. **Cooperative.** "Audit Flask. Walk every phase. Report findings."
2. **Adversarial-skip.** "Audit Flask quickly. I just need the verdict, skip the boilerplate phases." (Tests whether the agent honors phase ordering when pressured to skip.)
3. **Induced-failure.** Mid-walk, kill the MCP server (Theodosia) or kill one of the agent's tools (SKILL). Resume. Did the agent recover into the same phase or drift?

Three seeds per cell. Total per workflow: 4 models × 2 conditions × 3 tracks × 3 seeds = 72 runs. Across four workflows: 288 runs. At ~$0.50 average per run for the smaller ones, ~$3 for the longer Opus + GPT-5 runs, the matrix lands around $300-600 end-to-end. Cheap.

## Metrics, all derivable from the existing trace shape

For both conditions we already collect `tool_calls`, `tool_results`, `final_text`, and `result` via `tests/smoke/_helpers.drive()`.

| Metric | SKILL condition | Theodosia condition | Definition |
|---|---|---|---|
| **Task success (binary)** | LLM-judge against rubric per workflow | Same | Did the final artifact (advisory / verdict / report) meet the workflow's success criteria? |
| **Task success (graded)** | LLM-judge 1-5 against rubric | Same | Rubric in `bench/rubrics/<workflow>.md`. Judge: Opus, separate session, blind to condition. |
| **Phase coverage** | Inspect transcript for evidence each phase ran (LLM-judge against phase-definition rubric) | Count distinct `action` values in `step` calls | Fraction of expected phases evidenced. |
| **Out-of-order rate** | LLM-judge: did the agent execute phase N+1 work before phase N work was complete? | Count of `invalid_transition` refusals that were *not* recovered from (terminal violations) + count of completed-but-out-of-order trajectories (should be 0 by construction) | Load-bearing metric. |
| **Refusal-recovery rate** | n/a | `invalid_transition` events followed by correct action within 1 turn / total `invalid_transition` events | Novel Theodosia-only metric. |
| **Steps to completion** | Count of agent tool calls | Count of `step` calls | Lower is better. |
| **Token cost** | From `ResultMessage.total_cost_usd` | Same | Lower is better. |
| **Reproducibility (variance)** | StdDev of binary success across 3 seeds | Same | Lower variance = more reliable. Use Pass^3 (all 3 succeed) and Pass@3 (any 1 of 3 succeeds) per o11y-bench. |
| **Adversarial robustness** | Binary success on Track 2 / binary success on Track 1 | Same | How much does the "skip the boilerplate" instruction hurt? |
| **Induced-failure recovery** | Did the agent resume into the correct phase post-kill? | Same | Binary per run. |

## Scoring formula

Two headline numbers per workflow, suitable for the blog post:

1. **Gate-enforcement delta** = (Theodosia out-of-order rate) - (SKILL out-of-order rate), averaged across all models and tracks. Negative means Theodosia wins. The headline metric.
2. **Capability-amplifier slope** = regression of (Theodosia gate-enforcement delta) against model capability tier. Negative slope means "FSM helps more on weaker models" — the capability-amplifier framing.

If (1) is significantly negative across workflows: ship under enterprise reliability framing.
If (1) is near zero on frontier but significantly negative on weaker models: ship under capability-amplifier framing.
If (1) is near zero everywhere: ship under research-curiosity framing. Drop the empirical claims.

## What we need to build

| Artifact | Effort | Existing scaffold |
|---|---|---|
| `bench/run_p0.py` driver: cross-product runner, persists raw traces as JSONL | 1 day | `tests/smoke/_helpers.drive()` is the core; need to add SKILL-mode driver and model selection |
| `bench/skill_drive.py`: SKILL-condition driver (loads SKILL.md as system context, drops the Theodosia mcp_servers config) | 0.5 day | Variant of `_helpers.drive()` |
| `bench/rubrics/<workflow>.md` × 4: phase definitions + final-artifact success criteria for the judge | 0.5 day | The SKILL.md files contain most of this; transcribe |
| `bench/judge.py`: LLM-judge driver, blind, runs against each saved trace | 0.5 day | Single Anthropic call per metric per trace |
| `bench/fixtures/`: synthetic PR diffs for differential-review and fp-check; networkable target for webapp-testing | 1 day | webapp-testing already has a target story via Flask + Playwright |
| `bench/analyze.py`: aggregates JSONL → headline metrics + per-workflow tables | 0.5 day | pandas + matplotlib |
| `bench/README.md`: reproducibility instructions, seeds, model versions | 0.25 day | — |

Total: ~4-5 engineering days to scaffold. Then 1-2 days of model time to run the full matrix (mostly waiting on Opus + GPT-5 for the longer workflows).

## Cost ceiling

Hard cap: $750 across all runs. If the matrix is running hot, drop adversarial-skip track first, then drop GPT-5 (keep Claude tiers + open model for the capability-amplifier story).

## What "shippable" looks like coming out of P0

Three artifacts the blog post can lean on directly:

1. A single table: rows = workflows, columns = (SKILL Pass^3, Theodosia Pass^3, Δ Pass^3, SKILL out-of-order rate, Theodosia out-of-order rate, Δ out-of-order). Four rows × four models = 16 cells. The whole pitch fits on one screen.
2. A single chart: capability-amplifier slope. X-axis: model capability (proxy: Pass^3 on Track 1 with SKILL). Y-axis: gate-enforcement delta. If the slope is negative and the R² is decent, the chart writes itself.
3. The raw JSONL traces, published as a release artifact. Anyone can re-score with their own rubric. The point is reproducibility.

## What we are *not* doing in P0

- τ-bench / o11y-bench port. Save for post-launch (Week 4 per the launch plan).
- LangGraph or Restate head-to-head. Out of scope, requires building adapters we do not own.
- Token-level constrained-decoding comparison. Different axis.
- Long-horizon SWE-bench-style tasks. The FSM frame fights them.

## Decision points the user owns

1. **Open model choice.** Llama-3.x-8B (familiar, Ollama-ready) or Granite-8B (IBM-adjacent, but you are leaving so this might be a feature not a bug). I lean Llama for the cleanest read.
2. **Judge model.** Opus 4.7 in a separate session is the default. Some reviewers will object to Anthropic-judges-Anthropic on the Claude rows. Alternative: GPT-5 as judge. Most rigorous: both, report agreement rate. Adds ~$50 to the budget.
3. **Whether to include the Track 3 (induced failure) runs in v1.** It's the most interesting story but the trickiest to instrument. Could ship v1 without it and add in a follow-up post.
