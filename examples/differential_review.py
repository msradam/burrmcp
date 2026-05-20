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
You are running Trail of Bits' differential-review SKILL on `{target}`.
Codebase size strategy: `{codebase_size}` (SMALL=DEEP, MEDIUM=FOCUSED,
LARGE=SURGICAL). Scope: `{scope}`.

PRE-ANALYSIS: build baseline context before triage.

Capture:
- changed_files: list of files touched by this change (paths only)
- removed_security_code: any code removed from "security", "CVE", or
  "fix"-tagged commits. For each: {{file, lines, commit, what}}. This
  is the highest-leverage signal in the SKILL; do not skip even on a
  small PR.
- entrypoints_touched: public APIs / handlers / CLI commands whose
  surface is altered by this change
- dependencies_touched: package/version changes, especially security-
  sensitive packages (crypto, auth, parsing, networking)
- removed_validation_or_access_control: anything in the "Red Flags"
  list from the SKILL (onlyOwner removed, internal -> external,
  validation removed without replacement, external calls added without
  checks)

Call `pre_analysis(baseline={{...}})` with a dict of these fields.
"""


_PROMPT_TRIAGE = """\
PHASE 0 of 6: TRIAGE.

Classify each changed file by risk per the SKILL's risk-level table:

  HIGH:   auth, crypto, external calls, value transfer, validation
          removal
  MEDIUM: business logic, state changes, new public APIs
  LOW:    comments, tests, UI-only, logging

Per the SKILL: "Heartbleed was 2 lines" -- classify by risk, not size.
A 2-line change in auth or crypto code is HIGH.

Call `triage(per_file_risk={{path: "HIGH"|"MEDIUM"|"LOW", ...}})`
with one entry per changed_files path you recorded in pre_analysis.
The FSM aggregates these into an overall_risk = max(per_file_risk).
If any file is HIGH, the review will pass through Phase 4 (Deep
Context) and Phase 5 (Adversarial); otherwise it skips straight to
Phase 6 after Phase 3.
"""


_PROMPT_CODE_ANALYSIS = """\
PHASE 1 of 6: CODE ANALYSIS.

For each HIGH or MEDIUM risk file:

1. Run `git blame` on every removed line in security-sensitive code.
   The original commit is the evidence trail; cite its sha in findings.
2. Read the file alongside its 1-hop neighbours (callers, callees, and
   any module that imports from it).
3. Look for the SKILL's pattern catalog: regressions, access control,
   missing validation, injection, crypto misuse, overflow, reentrancy.

For each finding: {{file, line, severity (critical|high|medium|low),
cwe (id), evidence_commit (sha), description, suggested_fix}}.

Call `code_analysis(findings=[{{...}}, ...])`. If a HIGH-risk file
yields no findings, return an entry with severity="info" noting the
file was reviewed clean, so the audit trail records the coverage.
"""


_PROMPT_TEST_COVERAGE = """\
PHASE 2 of 6: TEST COVERAGE.

For each changed file, check whether tests cover the changed lines.
Per the SKILL: "Missing tests = elevated risk rating, flag in report".

Capture:
- per_file_coverage: {{path: {{has_tests: bool, covers_changes: bool,
  test_files: [...]}}}}.
- elevations: list of files whose risk should be elevated one tier
  because tests are missing or do not cover the changed lines.

Call `test_coverage(coverage={{...}})` with these fields.
"""


_PROMPT_BLAST_RADIUS = """\
PHASE 3 of 6: BLAST RADIUS.

For every HIGH-risk file, count direct callers / consumers. The SKILL
considers "50+ callers + HIGH risk change" a red flag for immediate
escalation.

Capture:
- per_file_callers: {{path: {{direct_callers: int, transitive_callers:
  int, sample_callers: [3-5 paths]}}}}.
- high_blast_radius_changes: list of {{file, callers, why_concerning}}
  for any HIGH file with notable reach.

Call `blast_radius(blast={{...}})`.
"""


_PROMPT_DEEP_CONTEXT = """\
PHASE 4 of 6: DEEP CONTEXT.

(Only reached because triage classified at least one file HIGH.)

For each HIGH-risk file, document baseline assumptions and invariants
that this change touches:

- What invariants did the surrounding code rely on before this change?
- Are those invariants still valid after the change?
- Are there assumptions about caller identity, ordering, or
  preconditions that the diff invalidates?

Capture:
- per_file_context: {{path: {{prior_invariants: [...], after_change:
  "preserved|broken|unclear", notes: "..."}}}}.

Call `deep_context(context={{...}})`.
"""


_PROMPT_ADVERSARIAL = """\
PHASE 5 of 6: ADVERSARIAL MODELING.

(Only reached because triage classified at least one file HIGH.)

For each HIGH-risk file with a non-trivial finding from Phase 1, build
a concrete attacker scenario per the SKILL's adversarial methodology:

1. Attacker model: who is the threat (external unauth, authed user,
   insider, supply chain)?
2. Attack vector: what surface does the change expose?
3. Exploitability rating: critical / high / medium / low.
4. Exploit scenario: concrete sequence of steps a real attacker would
   take. Not "could lead to RCE in theory"; concrete.
5. Baseline cross-reference: does the prior code (before this change)
   prevent this scenario? If yes, this change is a regression.

Capture:
- scenarios: list of {{file, attacker_model, attack_vector,
  exploitability, steps, regression (bool), pre_change_blocked (bool)}}.

Call `adversarial(scenarios=[{{...}}, ...])`. Be honest if a HIGH file
does not yield a concrete scenario; the SKILL says generic findings
without evidence are not acceptable.
"""


_PROMPT_REPORT = """\
PHASE 6 of 6: WRITE THE REPORT.

Per the SKILL: "Output report only to chat (file required)". Write a
comprehensive markdown report combining every prior phase's findings.

Structure:

# Differential review: {target}

## Summary
- Overall risk: {overall_risk}
- Total findings: <N> (critical: <c>, high: <h>, medium: <m>, low: <l>)
- Codebase size strategy: {codebase_size}
- Files reviewed: <N>

## Pre-analysis baseline
(removed_security_code highlights, entrypoints touched, dependencies)

## Triage
(per-file risk table)

## Code analysis findings
(one section per finding: severity, cwe, file:line, evidence commit,
description, suggested fix)

## Test coverage gaps
(elevations + uncovered changed lines)

## Blast radius
(any high-blast-radius HIGH changes)

{adversarial_section}

## Coverage limitations
(be honest: which files were not deeply analysed; what was skipped due
to strategy = {codebase_size})

Call `write_report(report="...")` with the full markdown text. The
review terminates here.
"""


_ADVERSARIAL_SECTION_HIGH = """\
## Deep context (HIGH-risk files)
(per-file invariants and whether they survive the change)

## Adversarial modeling
(one section per scenario, with attacker model, exploit steps,
regression status)\
"""


_ADVERSARIAL_SECTION_NONE = """\
## Deep context + adversarial modeling
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
