"""Skill-to-FSM: verify a suspected security bug as a true or false positive.

Trail of Bits' ``fp-check`` SKILL decomposed into a Burr FSM. Each
phase emits a structured prompt for the caller LLM, the LLM responds
with structured evidence, and the FSM gates the next phase so the
six mandatory gate reviews from the SKILL's ``gate-reviews.md`` cannot
be skipped or reordered.

Source SKILL: ``examples/skills/fp-check/SKILL.md`` (CC BY-SA 4.0,
github.com/trailofbits/skills). Phase ordering mirrors the SKILL:

    start_check -> step0_restate (mandatory precondition; the SKILL
        says half of false positives collapse here) ->
    route_path (Standard vs Deep, default Standard) ->
    gate1_process -> gate2_reachability -> gate3_impact ->
    gate4_poc_validation -> gate5_math_bounds ->
    gate6_environment -> final_verdict

The verdict policy from ``gate-reviews.md`` is enforced in
``final_verdict``: TRUE POSITIVE only when all six gates passed,
FALSE POSITIVE if any failed. The SKILL says to keep going through
all phases even after a failure so the verdict carries the specific
gate that rejected it.

Complementary to ``security_audit.py`` (finds bugs) and
``differential_review.py`` (reviews changes). Where those find
findings, this verifies whether a specific finding is real.

Run:

    uv run python examples/fp_check.py
"""

from __future__ import annotations

from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "fp-check-demo"

_VALID_VERDICTS = {"pass", "fail"}
_VALID_PATHS = {"standard", "deep"}


# == prompt templates (drawn from fp-check SKILL + gate-reviews.md) =


_PROMPT_STEP0 = """\
Trail of Bits fp-check SKILL. Original claim:

    {bug_summary}

STEP 0: UNDERSTAND THE CLAIM AND CONTEXT.

From `SKILL.md#step-0-understand-the-claim-and-context`: "Half of
false positives collapse at this step -- the claim doesn't make
coherent sense when restated precisely." Restate the bug in your own
words and document (verbatim list from the SKILL):

- exact_claim: e.g. "heap buffer overflow in parse_header() when
  content_length exceeds 4096"
- alleged_root_cause: e.g. "missing bounds check before memcpy at
  line 142"
- supposed_trigger: e.g. "attacker sends HTTP request with oversized
  Content-Length header"
- claimed_impact: e.g. "remote code execution via controlled heap
  corruption"
- threat_model: privilege level, sandbox, attacker preconditions
- bug_class: classify and consult
  `bug-class-verification.md` for class-specific requirements
- execution_context: when is this code path reached during normal
  execution?
- caller_analysis: what functions call this code, what input
  constraints do they impose?
- architectural_context: is this part of a larger security system
  with multiple protection layers?
- historical_context: recent changes, known issues, previous reviews
- can_restate_clearly: bool. The SKILL: "If you cannot do this
  clearly, ask the user for clarification using AskUserQuestion."

Call `step0_restate(restated={{...}})`. The FSM refuses to advance
when can_restate_clearly is False; the SKILL forbids guessing past
Step 0.
"""


_PROMPT_ROUTE = """\
ROUTE: STANDARD VS DEEP VERIFICATION.

From `SKILL.md#route-standard-vs-deep-verification`:

Use `standard` (linear single-pass checklist, see
`standard-verification.md`) when ALL hold:
  - Clear, specific vulnerability claim (not vague or ambiguous)
  - Single component -- no cross-component interaction in bug path
  - Well-understood bug class (buffer overflow, SQL injection, XSS,
    integer overflow, etc.)
  - No concurrency or async involved in the trigger
  - Straightforward data flow from source to sink

Use `deep` (full task-based orchestration, see `deep-verification.md`)
when ANY hold:
  - Ambiguous claim that could be interpreted multiple ways
  - Cross-component bug path (data flows through 3+ modules / services)
  - Race conditions, TOCTOU, concurrency in the trigger mechanism
  - Logic bugs without a clear spec to verify against
  - Standard verification was inconclusive or escalated
  - User explicitly requests full verification

Default: `standard`. Standard verification has two built-in escalation
checkpoints that route to deep when complexity exceeds the linear
checklist.

Call `route_path(path="standard"|"deep", justification="...")`. The
same six gates from `gate-reviews.md` fire on both paths; the path
controls how you collect evidence, not which gates apply.
"""


_GATE_PROMPT_PREFACE = """\
GATE REVIEW {gate_num} of 6: {gate_name}.

Criterion (from gate-reviews.md):

    {criterion}

PASS when: {pass_criterion}
FAIL when: {fail_criterion}

The SKILL forbids partial analysis: even if you suspect this gate
will fail, document the evidence completely and continue to the next
gate. Final verdict is computed only after all six gates have fired.
"""


_PROMPT_GATE1 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=1,
        gate_name="PROCESS",
        criterion="All phases completed with documented evidence.",
        pass_criterion="Evidence exists for every phase the chosen path requires.",
        fail_criterion="Phases lack concrete evidence (hand-waving, pattern matching only).",
    )
    + """

Call `gate1_process(verdict="pass"|"fail", evidence={...})`. Evidence
should list which phases produced concrete artefacts (data-flow trace,
attacker-control proof, etc.) so a third party can audit your work.
"""
)


_PROMPT_GATE2 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=2,
        gate_name="REACHABILITY",
        criterion="Attacker can reach and control data at the vulnerability.",
        pass_criterion="Clear attacker-controlled path + PoC confirms reachability.",
        fail_criterion="Cannot demonstrate attacker control or the path is unreachable.",
    )
    + """

Call `gate2_reachability(verdict="pass"|"fail", evidence={...})`.
Evidence: data-flow trace from attacker source to the vulnerable sink,
naming each intermediate function and any validation crossed.
"""
)


_PROMPT_GATE3 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=3,
        gate_name="REAL IMPACT",
        criterion="Exploitation leads to RCE, privesc, or info disclosure.",
        pass_criterion="Direct impact with concrete scenarios.",
        fail_criterion="Only an operational robustness issue (crash with no security impact).",
    )
    + """

Call `gate3_impact(verdict="pass"|"fail", evidence={...})`. Evidence:
which CIA properties are violated and the concrete attacker objective
achieved.
"""
)


_PROMPT_GATE4 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=4,
        gate_name="POC VALIDATION",
        criterion="A PoC (pseudocode, executable, or unit test) demonstrates the attack path.",
        pass_criterion="PoC shows attacker control, the trigger, and the impact end-to-end.",
        fail_criterion="PoC fails to show the attack path or the impact.",
    )
    + """

Call `gate4_poc_validation(verdict="pass"|"fail", evidence={...})`.
Evidence: the PoC itself (or a reference to it) plus a brief
explanation of what each step demonstrates.
"""
)


_PROMPT_GATE5 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=5,
        gate_name="MATH BOUNDS",
        criterion="Mathematical analysis confirms the vulnerable condition is possible.",
        pass_criterion="Algebraic proof shows the condition can occur.",
        fail_criterion="Math proves validation prevents it (the original claim is impossible).",
    )
    + """

Call `gate5_math_bounds(verdict="pass"|"fail", evidence={...})`.
Evidence: explicit inequalities, the variables involved, and the
range each can take. Cite line numbers for any validation that
constrains the math.
"""
)


_PROMPT_GATE6 = (
    _GATE_PROMPT_PREFACE.format(
        gate_num=6,
        gate_name="ENVIRONMENT",
        criterion="No environmental protections entirely prevent exploitation.",
        pass_criterion=(
            "Protections (ASLR, stack canaries, sandboxes, network ACLs) do not "
            "eliminate the vulnerability."
        ),
        fail_criterion="An environmental protection blocks exploitation entirely.",
    )
    + """

Call `gate6_environment(verdict="pass"|"fail", evidence={...})`.
Evidence: which protections apply in deployment + how they interact
with the alleged exploitation path.
"""
)


_PROMPT_FINAL = """\
STEP 8 of 8: FINAL VERDICT.

You have completed all six gate reviews. The SKILL's verdict policy
is mechanical: TRUE POSITIVE iff all six gates passed; FALSE POSITIVE
otherwise. The FSM computes the verdict from your gate verdicts; you
only need to provide a final summary.

Call `final_verdict(notes="...")` with:
- A one-line summary of the verdict and the load-bearing gate (for
  FALSE POSITIVE, this is the first gate that failed; for TRUE
  POSITIVE, the strongest piece of evidence).
- For FALSE POSITIVE: a short reason for rejection.
- For TRUE POSITIVE: a short vulnerability description.

The FSM stores the computed verdict in state.verdict and the
documentary trail in burr://history.
"""


# == actions =========================================================


@action(
    reads=[],
    writes=[
        "bug_summary",
        "restated",
        "path",
        "path_justification",
        "gate_results",
        "verdict",
        "verdict_summary",
        "current_prompt",
        "log",
    ],
)
def start_check(state: State, bug_summary: str) -> State:
    """Open a verification session for one suspected bug.

    Args:
        bug_summary: One- or two-sentence description of the bug as
            initially reported. The agent will restate it in Step 0.
    """
    if not bug_summary.strip():
        raise ValueError("bug_summary must not be empty")
    return state.update(
        bug_summary=bug_summary,
        restated={},
        path="",
        path_justification="",
        gate_results={},
        verdict=None,
        verdict_summary=None,
        current_prompt=_PROMPT_STEP0.format(bug_summary=bug_summary),
        log=[f"Verification started: bug_summary={bug_summary!r}"],
    )


@action(reads=["log"], writes=["restated", "current_prompt", "log"])
def step0_restate(state: State, restated: dict[str, Any]) -> State:
    """Stash the restated claim. Refuses when can_restate_clearly is False.

    The SKILL: "If you cannot do this clearly, ask the user for
    clarification using AskUserQuestion." The FSM enforces this by
    refusing to advance.
    """
    required = [
        "exact_claim",
        "alleged_root_cause",
        "supposed_trigger",
        "claimed_impact",
        "threat_model",
        "bug_class",
        "can_restate_clearly",
    ]
    missing = [k for k in required if k not in restated]
    if missing:
        raise ValueError(f"restated is missing required keys: {missing}")
    if not restated.get("can_restate_clearly"):
        raise ValueError(
            "can_restate_clearly is False; the SKILL forbids proceeding past "
            "Step 0 with an unclear claim. Ask the user for clarification, "
            "rewrite the bug_summary, and call start_check again."
        )
    return state.update(
        restated=restated,
        current_prompt=_PROMPT_ROUTE,
        log=[*state["log"], f"Step 0 restated; bug_class={restated.get('bug_class')!r}"],
    )


@action(reads=["log"], writes=["path", "path_justification", "current_prompt", "log"])
def route_path(
    state: State,
    path: Literal["standard", "deep"],
    justification: str = "",
) -> State:
    """Choose Standard or Deep verification path."""
    if path not in _VALID_PATHS:
        raise ValueError(f"path must be one of {sorted(_VALID_PATHS)}; got {path!r}")
    return state.update(
        path=path,
        path_justification=justification,
        current_prompt=_PROMPT_GATE1,
        log=[*state["log"], f"Routed to {path} verification"],
    )


def _record_gate(
    state: State,
    *,
    gate_num: int,
    gate_name: str,
    verdict: str,
    evidence: dict[str, Any],
    next_prompt: str,
) -> State:
    """Record one gate's outcome and advance to the next prompt.

    Stores the gate result on ``state.gate_results`` keyed by gate
    name. The final_verdict action reads this dict to compute the
    overall TRUE/FALSE POSITIVE verdict.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_VALID_VERDICTS)}; got {verdict!r}")
    results = {**state["gate_results"], gate_name: {"verdict": verdict, "evidence": evidence}}
    return state.update(
        gate_results=results,
        current_prompt=next_prompt,
        log=[*state["log"], f"Gate {gate_num} ({gate_name}) recorded: {verdict}"],
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate1_process(
    state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]
) -> State:
    """Gate 1: Process. All phases completed with documented evidence."""
    return _record_gate(
        state,
        gate_num=1,
        gate_name="process",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_GATE2,
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate2_reachability(
    state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]
) -> State:
    """Gate 2: Reachability. Attacker control + reachability evidence."""
    return _record_gate(
        state,
        gate_num=2,
        gate_name="reachability",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_GATE3,
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate3_impact(state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]) -> State:
    """Gate 3: Real Impact. RCE, privesc, info disclosure, not robustness."""
    return _record_gate(
        state,
        gate_num=3,
        gate_name="impact",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_GATE4,
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate4_poc_validation(
    state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]
) -> State:
    """Gate 4: PoC. End-to-end demonstration of the attack path."""
    return _record_gate(
        state,
        gate_num=4,
        gate_name="poc",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_GATE5,
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate5_math_bounds(
    state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]
) -> State:
    """Gate 5: Math Bounds. Algebraic proof condition is possible."""
    return _record_gate(
        state,
        gate_num=5,
        gate_name="math",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_GATE6,
    )


@action(reads=["gate_results", "log"], writes=["gate_results", "current_prompt", "log"])
def gate6_environment(
    state: State, verdict: Literal["pass", "fail"], evidence: dict[str, Any]
) -> State:
    """Gate 6: Environment. Protections don't entirely prevent exploitation."""
    return _record_gate(
        state,
        gate_num=6,
        gate_name="environment",
        verdict=verdict,
        evidence=evidence,
        next_prompt=_PROMPT_FINAL,
    )


_GATE_ORDER = ["process", "reachability", "impact", "poc", "math", "environment"]


@action(
    reads=["gate_results", "bug_summary", "log"],
    writes=["verdict", "verdict_summary", "current_prompt", "log"],
)
def final_verdict(state: State, notes: str = "") -> State:
    """Terminal: compute TRUE / FALSE POSITIVE per gate-reviews.md.

    TRUE POSITIVE only when every gate passed. FALSE POSITIVE
    otherwise; the verdict_summary records which gate(s) failed so
    the audit trail carries the load-bearing rejection reason.
    """
    results = state["gate_results"]
    missing = [g for g in _GATE_ORDER if g not in results]
    if missing:
        raise ValueError(
            f"final_verdict cannot fire: gates {missing} have no recorded "
            "outcome. The SKILL requires all six gates before the verdict."
        )
    failed = [g for g in _GATE_ORDER if results[g]["verdict"] == "fail"]
    is_true_positive = not failed
    verdict = "TRUE POSITIVE" if is_true_positive else "FALSE POSITIVE"
    summary = {
        "verdict": verdict,
        "passed_gates": [g for g in _GATE_ORDER if results[g]["verdict"] == "pass"],
        "failed_gates": failed,
        "load_bearing_gate": failed[0] if failed else None,
        "notes": notes,
    }
    return state.update(
        verdict=verdict,
        verdict_summary=summary,
        current_prompt=f"Verification complete: {verdict}. See state.verdict_summary.",
        log=[
            *state["log"],
            f"Final verdict: {verdict}" + (f" (failed at gate {failed[0]})" if failed else ""),
        ],
    )


# == graph ===========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_check=start_check,
            step0_restate=step0_restate,
            route_path=route_path,
            gate1_process=gate1_process,
            gate2_reachability=gate2_reachability,
            gate3_impact=gate3_impact,
            gate4_poc_validation=gate4_poc_validation,
            gate5_math_bounds=gate5_math_bounds,
            gate6_environment=gate6_environment,
            final_verdict=final_verdict,
        )
        .with_transitions(
            ("start_check", "step0_restate"),
            ("step0_restate", "route_path"),
            ("route_path", "gate1_process"),
            ("gate1_process", "gate2_reachability"),
            ("gate2_reachability", "gate3_impact"),
            ("gate3_impact", "gate4_poc_validation"),
            ("gate4_poc_validation", "gate5_math_bounds"),
            ("gate5_math_bounds", "gate6_environment"),
            ("gate6_environment", "final_verdict"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            bug_summary="",
            restated={},
            path="",
            path_justification="",
            gate_results={},
            verdict=None,
            verdict_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_check")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="fp-check",
        instructions=(
            "Trail of Bits' fp-check SKILL decomposed into an FSM. "
            "Verify a single suspected security bug as TRUE POSITIVE "
            "or FALSE POSITIVE through six mandatory gate reviews. "
            "The CALLER LLM does the analysis; this FSM gates the "
            "phase order. Walk: start_check(bug_summary) -> "
            "step0_restate (mandatory; refuses on unclear claim) -> "
            "route_path (standard or deep) -> gate1_process -> "
            "gate2_reachability -> gate3_impact -> "
            "gate4_poc_validation -> gate5_math_bounds -> "
            "gate6_environment -> final_verdict. The verdict is "
            "mechanically computed: TRUE POSITIVE iff every gate "
            "passed, FALSE POSITIVE otherwise. Read state.current_prompt "
            "for the next phase's evidence checklist. Complementary "
            "to security_audit (finds bugs) and "
            "differential_review (reviews changes): audit finds, this "
            "verifies. Source SKILL at examples/skills/fp-check/."
        ),
    )


if __name__ == "__main__":
    build_server().run()
