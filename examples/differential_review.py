"""Skill-to-FSM: differential security review as caller-LLM prompts.

Trail of Bits' ``differential-review`` SKILL decomposed into a Burr
FSM. Each phase becomes one action that emits a structured prompt
for the *caller* LLM (Opus, Sonnet, Granite, whichever is driving
BurrMCP) and stores the caller's structured response in state. The
FSM does not call any LLM; it just gates which phase is reachable
next.

Source SKILL: ``examples/skills/differential-review/SKILL.md``
(CC BY-SA 4.0, github.com/trailofbits/skills). The phase ordering
and risk-level gate below mirror the SKILL's own workflow:

    Pre-Analysis -> Phase 0 (Triage) -> Phase 1 (Code Analysis) ->
    Phase 2 (Test Coverage) -> Phase 3 (Blast Radius) ->
        if HIGH risk:
            Phase 4 (Deep Context) -> Phase 5 (Adversarial) -> Phase 6 (Report)
        else:
            Phase 6 (Report)

Why this is a clean FSM:

* Risk-level gate is enforced at the protocol layer. The caller LLM
  cannot skip Phase 5 on a HIGH-risk change because phases 4 and 5
  are only reachable when ``state.overall_risk == "HIGH"``.
* Pre-Analysis cannot be skipped. The SKILL's "rationalisations to
  not skip" table calls this out as the most common bypass; the
  FSM refuses ``triage`` until ``pre_analysis`` has fired.
* The report can only be written once every prior phase has stashed
  its findings into state. ``write_report`` reads from every prior
  phase's slot; the action body refuses if any required slot is
  empty (for non-HIGH paths, the deep-context + adversarial slots
  are intentionally allowed to be empty).
* Every prompt + every response lands in ``burr://history``; the
  audit trail is the artefact, same as the security-audit demo.

Pure FSM. No server-side LLM calls, no shellouts. Complementary to
``codebase_security.py`` (real bandit + detect-secrets) and to
``skill_security_audit.py`` (audit a single target). Where those
two find new findings, this one reviews changes for security
regressions.

Run:

    uv run python examples/differential_review.py
"""

from __future__ import annotations

from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "differential-review-demo"

_VALID_RISK_LABELS = {"HIGH", "MEDIUM", "LOW"}
_VALID_CODEBASE_SIZES = {"SMALL", "MEDIUM", "LARGE"}


# == prompt templates (drawn from the differential-review SKILL) ====


_PROMPT_PRE_ANALYSIS = """\
Trail of Bits differential-review SKILL on `{target}`. Codebase size:
`{codebase_size}` (SMALL=DEEP, MEDIUM=FOCUSED, LARGE=SURGICAL).
Scope: `{scope}`.

PRE-ANALYSIS: Baseline Context Building.

Per `methodology.md#pre-analysis-baseline-context-building`, build
baseline context before walking the phases. The SKILL invokes the
`audit-context-building` sub-skill against the baseline commit; if
that sub-skill isn't available, document the same surface from
`git log` / file listing.

Also capture the SKILL's `Red Flags (Stop and Investigate)`:
- Removed code from "security", "CVE", or "fix" commits
- Access control modifiers removed (onlyOwner, internal -> external)
- Validation removed without replacement
- External calls added without checks
- High blast radius (50+ callers) + HIGH risk change

Call `pre_analysis(baseline={{...}})` with:
- changed_files: list of files touched by this change
- removed_security_code: [{{file, lines, commit, what}}, ...] for any
  code removed from "security" / "CVE" / "fix" commits
- entrypoints_touched: public APIs / handlers / CLI commands altered
- dependencies_touched: security-sensitive package / version changes
- red_flags_hit: which Red Flags from the SKILL list this PR triggers
"""


_PROMPT_TRIAGE = """\
PHASE 0: INTAKE & TRIAGE.

From `methodology.md#phase-0-intake--triage`, risk-score each file:

  HIGH:   Auth, crypto, external calls, value transfer, validation removal
  MEDIUM: Business logic, state changes, new public APIs
  LOW:    Comments, tests, UI, logging

Per the SKILL's `Rationalizations (Do Not Skip)` table: "Heartbleed
was 2 lines" -- classify by RISK, not size.

Call `triage(per_file_risk={{path: "HIGH"|"MEDIUM"|"LOW", ...}})` with
one entry per file in pre_analysis.changed_files. The FSM aggregates
to overall_risk = max(per_file_risk). HIGH triggers Phases 4 (Deep
Context) and 5 (Adversarial); otherwise the workflow goes straight
from Phase 3 to Phase 6.
"""


_PROMPT_CODE_ANALYSIS = """\
PHASE 1: CHANGED CODE ANALYSIS.

From `methodology.md#phase-1-changed-code-analysis`, for each changed
file:

1. Read both versions (baseline and changed).
2. Analyze each diff region with BEFORE / AFTER / CHANGE / SECURITY.
3. Git blame removed code: `git log -S "removed_code" --all --oneline`.
   Red flags: removed from "fix"/"security"/"CVE" commits = CRITICAL;
   recently added (<1 month) then removed = HIGH.
4. Check for regressions (re-added code): `git log -S "added_code"
   --all -p`. Pattern: code added -> removed for security -> re-added
   now = REGRESSION.
5. Micro-adversarial analysis for each change: what attack did removed
   code prevent? what new surface does new code expose? can modified
   logic be bypassed? are checks weaker?
6. For each change with concern, generate a concrete attack scenario
   with SCENARIO / PRECONDITIONS / STEPS / WHY IT WORKS / IMPACT.

Apply pattern catalogue from `patterns.md` (regressions, reentrancy,
access control, overflow, etc.).

Call `code_analysis(findings=[{{file, line, severity, cwe,
evidence_commit, description, suggested_fix}}, ...])`. An HIGH-risk
file reviewed clean should still get one entry with severity="info"
so coverage is recorded.
"""


_PROMPT_TEST_COVERAGE = """\
PHASE 2: TEST COVERAGE ANALYSIS.

From `methodology.md#phase-2-test-coverage-analysis`, apply the
SKILL's Risk Elevation Rules:

- NEW function + NO tests -> elevate risk MEDIUM -> HIGH
- MODIFIED validation + UNCHANGED tests -> HIGH RISK
- Complex logic (>20 lines) + NO tests -> HIGH RISK

Call `test_coverage(coverage={{per_file_coverage, elevations}})`
where:
- per_file_coverage: {{path: {{has_tests, covers_changes, test_files}}}}
- elevations: paths whose risk should be elevated per the rules above
"""


_PROMPT_BLAST_RADIUS = """\
PHASE 3: BLAST RADIUS ANALYSIS.

From `methodology.md#phase-3-blast-radius-analysis`, count callers
per modified function:

  1-5 calls   = LOW
  6-20        = MEDIUM
  21-50       = HIGH
  50+         = CRITICAL

The SKILL's Priority Matrix combines blast radius with change risk:
HIGH change + CRITICAL blast = P0 (deep + all deps); HIGH change +
HIGH/MEDIUM blast = P1 (deep); HIGH + LOW = P2 (standard); MEDIUM +
CRITICAL/HIGH = P1 (standard + callers).

Call `blast_radius(blast={{per_file_callers, high_blast_radius_changes,
priority_matrix}})`.
"""


_PROMPT_DEEP_CONTEXT = """\
PHASE 4: DEEP CONTEXT ANALYSIS.

(Reachable only when triage classified at least one file HIGH.)

From `methodology.md#phase-4-deep-context-analysis`, for each
HIGH-risk file invoke (or simulate) the `audit-context-building`
sub-skill on the changed function and its dependencies. Document:

- prior invariants the surrounding code relied on
- whether those invariants survive the change ("preserved" / "broken"
  / "unclear")
- repeated validation patterns and whether any are removed by this
  diff

Call `deep_context(context={{per_file_context}})`.
"""


_PROMPT_ADVERSARIAL = """\
PHASE 5: ADVERSARIAL VULNERABILITY ANALYSIS.

(Reachable only on HIGH-risk reviews.)

From `adversarial.md`, follow the five-step adversarial methodology:

1. Define Specific Attacker Model: WHO is the attacker? WHAT
   access/privileges? WHERE do they interact with the system?
2. Identify Concrete Attack Vectors: ENTRY POINT, ATTACK SEQUENCE,
   PROOF OF ACCESSIBILITY.
3. Rate Realistic Exploitability: EASY (public APIs, no privileges)
   / MEDIUM (specific conditions or elevated privileges) / HARD
   (privileged access or rare conditions).
4. Build Complete Exploit Scenario: ATTACKER STARTING POSITION ->
   STEP-BY-STEP EXPLOITATION -> CONCRETE IMPACT (exact amount of
   funds drained / specific privileges escalated / particular data
   exposed -- not "could cause issues").
5. Cross-Reference with Baseline Context: does this violate a
   system-wide invariant? break a trust boundary? bypass a validation
   pattern? is it a regression of a previous fix?

Use the Vulnerability Report Template from `adversarial.md` for each
scenario.

Call `adversarial(scenarios=[{{file, attacker_model, attack_vector,
exploitability, steps, regression, pre_change_blocked}}, ...])`. The
SKILL forbids generic findings without evidence; be honest when a
HIGH file does not yield a concrete scenario.
"""


_PROMPT_REPORT = """\
PHASE 6: REPORT GENERATION.

From `reporting.md#report-structure`, write a comprehensive markdown
report. Per the SKILL: "Output report only to chat (file required)".

Required sections (per `reporting.md`):

# Differential review: {target}

## Executive Summary
- Overall risk: {overall_risk}
- Total findings: <N> (critical: <c>, high: <h>, medium: <m>, low: <l>)
- Codebase size strategy: {codebase_size}

## What Changed
(pre-analysis baseline + diff summary)

## Findings
(one section per finding from Phase 1: severity, CWE, file:line,
evidence commit, description, suggested fix)

## Test Coverage Analysis
(elevations + uncovered changed lines from Phase 2)

## Blast Radius Analysis
(Phase 3 priority matrix; high-blast-radius HIGH changes)

## Historical Context
(removed-then-re-added patterns; CVE-tagged removals)

{adversarial_section}

## Recommendations
(action items per finding)

## Analysis Methodology
(which phases ran; what `{codebase_size}` strategy meant for coverage)

Call `write_report(report="...")` with the full markdown text. Review
terminates here.
"""


_ADVERSARIAL_SECTION_HIGH = """\
## Deep Context (HIGH-risk files)
(per-file invariants and whether they survive the change, from Phase 4)

## Adversarial Analysis
(one section per scenario, with attacker model, exploit steps,
regression status, from Phase 5 / `adversarial.md`)\
"""


_ADVERSARIAL_SECTION_NONE = """\
## Deep Context + Adversarial Analysis
(skipped: overall risk is {overall_risk}, not HIGH; the SKILL
requires these phases only when at least one file is HIGH risk)\
"""


# == actions =========================================================


@action(
    reads=[],
    writes=[
        "target",
        "codebase_size",
        "scope",
        "baseline",
        "per_file_risk",
        "overall_risk",
        "code_findings",
        "coverage",
        "blast",
        "deep_context",
        "adversarial_scenarios",
        "report",
        "current_prompt",
        "log",
    ],
)
def start_review(
    state: State,
    target: str,
    codebase_size: Literal["SMALL", "MEDIUM", "LARGE"],
    scope: str = "",
) -> State:
    """Open a differential review.

    Args:
        target: PR id, commit sha, or diff identifier under review.
        codebase_size: SMALL (<20 files, DEEP strategy), MEDIUM (20-200,
            FOCUSED), LARGE (200+, SURGICAL). Per the SKILL's Codebase
            Size Strategy table.
        scope: free-text description of what's in scope (release tag,
            PR title, ticket id). Optional.
    """
    if not target.strip():
        raise ValueError("target must not be empty")
    if codebase_size not in _VALID_CODEBASE_SIZES:
        raise ValueError(
            f"codebase_size must be one of {sorted(_VALID_CODEBASE_SIZES)}; got {codebase_size!r}"
        )
    prompt = _PROMPT_PRE_ANALYSIS.format(
        target=target,
        codebase_size=codebase_size,
        scope=scope or "(not provided)",
    )
    return state.update(
        target=target,
        codebase_size=codebase_size,
        scope=scope,
        baseline={},
        per_file_risk={},
        overall_risk="UNKNOWN",
        code_findings=[],
        coverage={},
        blast={},
        deep_context={},
        adversarial_scenarios=[],
        report=None,
        current_prompt=prompt,
        log=[f"Review started: target={target!r}, codebase_size={codebase_size}"],
    )


@action(reads=["log"], writes=["baseline", "current_prompt", "log"])
def pre_analysis(state: State, baseline: dict[str, Any]) -> State:
    """Stash the baseline context the caller collected.

    Requires non-empty ``changed_files``; the SKILL forbids skipping
    pre-analysis even on small PRs.
    """
    changed = baseline.get("changed_files") or []
    if not changed:
        raise ValueError(
            "baseline.changed_files must list at least one file; "
            "the SKILL forbids skipping pre-analysis"
        )
    return state.update(
        baseline=baseline,
        current_prompt=_PROMPT_TRIAGE,
        log=[*state["log"], f"Pre-analysis recorded: {len(changed)} changed file(s)"],
    )


@action(
    reads=["baseline", "log"],
    writes=["per_file_risk", "overall_risk", "current_prompt", "log"],
)
def triage(state: State, per_file_risk: dict[str, str]) -> State:
    """Stash per-file risk classifications, derive overall_risk.

    The HIGH/MEDIUM/LOW labels are validated; anything else is refused.
    overall_risk = HIGH if any file is HIGH; else MEDIUM if any is
    MEDIUM; else LOW.
    """
    if not per_file_risk:
        raise ValueError("per_file_risk must classify at least one file")
    invalid = {
        path: label for path, label in per_file_risk.items() if label not in _VALID_RISK_LABELS
    }
    if invalid:
        raise ValueError(
            f"per_file_risk values must be one of {sorted(_VALID_RISK_LABELS)}; "
            f"got invalid {invalid}"
        )
    labels = set(per_file_risk.values())
    if "HIGH" in labels:
        overall = "HIGH"
    elif "MEDIUM" in labels:
        overall = "MEDIUM"
    else:
        overall = "LOW"
    return state.update(
        per_file_risk=dict(per_file_risk),
        overall_risk=overall,
        current_prompt=_PROMPT_CODE_ANALYSIS,
        log=[
            *state["log"],
            f"Triage recorded: {len(per_file_risk)} file(s), overall_risk={overall}",
        ],
    )


@action(reads=["log"], writes=["code_findings", "current_prompt", "log"])
def code_analysis(state: State, findings: list[dict[str, Any]]) -> State:
    """Stash code-analysis findings and emit the test-coverage prompt."""
    return state.update(
        code_findings=list(findings or []),
        current_prompt=_PROMPT_TEST_COVERAGE,
        log=[
            *state["log"],
            f"Code analysis recorded: {len(findings or [])} finding(s)",
        ],
    )


@action(reads=["log"], writes=["coverage", "current_prompt", "log"])
def test_coverage(state: State, coverage: dict[str, Any]) -> State:
    """Stash test-coverage notes and emit the blast-radius prompt."""
    return state.update(
        coverage=coverage,
        current_prompt=_PROMPT_BLAST_RADIUS,
        log=[*state["log"], "Test coverage recorded"],
    )


@action(
    reads=["overall_risk", "target", "codebase_size", "log"],
    writes=["blast", "current_prompt", "log"],
)
def blast_radius(state: State, blast: dict[str, Any]) -> State:
    """Stash blast-radius data; branch on overall_risk.

    HIGH risk: emit the deep-context prompt for Phase 4.
    MEDIUM/LOW: emit the report prompt directly; phases 4 and 5 are
    only required when the SKILL says so.
    """
    if state["overall_risk"] == "HIGH":
        next_prompt = _PROMPT_DEEP_CONTEXT
    else:
        next_prompt = _PROMPT_REPORT.format(
            target=state["target"],
            overall_risk=state["overall_risk"],
            codebase_size=state["codebase_size"],
            adversarial_section=_ADVERSARIAL_SECTION_NONE.format(
                overall_risk=state["overall_risk"]
            ),
        )
    return state.update(
        blast=blast,
        current_prompt=next_prompt,
        log=[
            *state["log"],
            f"Blast radius recorded; next phase: "
            f"{'deep_context' if state['overall_risk'] == 'HIGH' else 'write_report'}",
        ],
    )


@action(reads=["log"], writes=["deep_context", "current_prompt", "log"])
def deep_context(state: State, context: dict[str, Any]) -> State:
    """Stash deep-context notes and emit the adversarial prompt."""
    return state.update(
        deep_context=context,
        current_prompt=_PROMPT_ADVERSARIAL,
        log=[*state["log"], "Deep context recorded"],
    )


@action(
    reads=["target", "codebase_size", "overall_risk", "log"],
    writes=["adversarial_scenarios", "current_prompt", "log"],
)
def adversarial(state: State, scenarios: list[dict[str, Any]]) -> State:
    """Stash adversarial scenarios and emit the report prompt."""
    next_prompt = _PROMPT_REPORT.format(
        target=state["target"],
        overall_risk=state["overall_risk"],
        codebase_size=state["codebase_size"],
        adversarial_section=_ADVERSARIAL_SECTION_HIGH,
    )
    return state.update(
        adversarial_scenarios=list(scenarios or []),
        current_prompt=next_prompt,
        log=[
            *state["log"],
            f"Adversarial modeling recorded: {len(scenarios or [])} scenario(s)",
        ],
    )


@action(
    reads=[
        "target",
        "overall_risk",
        "code_findings",
        "coverage",
        "blast",
        "deep_context",
        "adversarial_scenarios",
        "log",
    ],
    writes=["report", "report_summary", "current_prompt", "log"],
)
def write_report(state: State, report: str) -> State:
    """Terminal: stash the report and build a per-phase summary."""
    if not report.strip():
        raise ValueError("report must not be empty; the SKILL requires an artefact")
    severities = [f.get("severity", "info") for f in state["code_findings"]]
    by_severity: dict[str, int] = {}
    for s in severities:
        by_severity[s] = by_severity.get(s, 0) + 1
    summary = {
        "target": state["target"],
        "overall_risk": state["overall_risk"],
        "total_findings": len(state["code_findings"]),
        "findings_by_severity": by_severity,
        "blast_radius_files": len(state.get("blast", {}).get("per_file_callers", {})),
        "adversarial_scenarios": len(state["adversarial_scenarios"]),
        "deep_context_files": len(state.get("deep_context", {}).get("per_file_context", {})),
    }
    return state.update(
        report=report,
        report_summary=summary,
        current_prompt="Review complete. Final report in state.report.",
        log=[
            *state["log"],
            f"Report written ({len(report)} chars). Review complete.",
        ],
    )


# == graph ===========================================================


_IS_HIGH_RISK = Condition.expr("overall_risk == 'HIGH'")
_NOT_HIGH_RISK = Condition.expr("overall_risk != 'HIGH'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_review=start_review,
            pre_analysis=pre_analysis,
            triage=triage,
            code_analysis=code_analysis,
            test_coverage=test_coverage,
            blast_radius=blast_radius,
            deep_context=deep_context,
            adversarial=adversarial,
            write_report=write_report,
        )
        .with_transitions(
            ("start_review", "pre_analysis"),
            ("pre_analysis", "triage"),
            ("triage", "code_analysis"),
            ("code_analysis", "test_coverage"),
            ("test_coverage", "blast_radius"),
            # Risk-level gate: HIGH continues into phases 4 + 5.
            ("blast_radius", "deep_context", _IS_HIGH_RISK),
            ("blast_radius", "write_report", _NOT_HIGH_RISK),
            ("deep_context", "adversarial"),
            ("adversarial", "write_report"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            target="",
            codebase_size="",
            scope="",
            baseline={},
            per_file_risk={},
            overall_risk="UNKNOWN",
            code_findings=[],
            coverage={},
            blast={},
            deep_context={},
            adversarial_scenarios=[],
            report=None,
            report_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_review")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="differential-review",
        instructions=(
            "Trail of Bits' differential-review SKILL decomposed into "
            "an FSM of prompts. The CALLER LLM (whoever drives you "
            "through MCP) does the thinking; this FSM emits one "
            "structured prompt per phase, stores your responses, and "
            "gates which phase is reachable next. Start with "
            "start_review(target, codebase_size, scope=''). Walk: "
            "pre_analysis -> triage -> code_analysis -> test_coverage "
            "-> blast_radius -> (if overall_risk == 'HIGH') "
            "deep_context -> adversarial -> write_report. Non-HIGH "
            "reviews skip phases 4 and 5 and go straight to "
            "write_report after blast_radius; the FSM enforces this "
            "at the transition layer so you cannot skip Phase 5 on a "
            "HIGH-risk change. Read state.current_prompt after each "
            "step for the next phase's checklist. Source SKILL at "
            "examples/skills/differential-review/."
        ),
    )


if __name__ == "__main__":
    build_server().run()
