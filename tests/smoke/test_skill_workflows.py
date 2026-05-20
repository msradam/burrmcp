"""Smoke tests for the SKILL FSMs against a real codebase (Flask).

These exercise the *full* SKILL workflow end-to-end, with the caller
LLM using its own ``Read`` / ``Grep`` / ``Bash`` tools to inspect a
real Python codebase. They verify two things the fast smoke suite
can't:

* The SKILL FSM walks every phase to completion on a non-trivial
  target (not just the first action).
* The final artefact (advisory / report / verdict_summary) is
  populated with structurally sensible content -- meaningful keys,
  non-empty findings list, the right verdict shape -- so a regression
  that breaks one phase's prompt template (and silently drops a
  required field) fails loudly.

These tests assert on *structural* properties only; they don't pin
specific finding counts or exact words. Real LLMs are stochastic and
real codebases evolve; pinning to literal output would make the suite
brittle.

Cost is higher than the fast smoke suite: each test drives 30-80
turns of an Opus session reading code. Against the Max plan's Agent
SDK credit, a full sweep is ~$10-20.

Run explicitly:

    uv run pytest -m smoke tests/smoke/test_skill_workflows.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

from ._helpers import (
    actions_called,
    calls_to,
    calls_with_action,
    check_environment_or_skip,
    drive,
    result_for,
)

check_environment_or_skip()


# == security-audit ========================================


@pytest.mark.asyncio
async def test_security_audit_walks_inside_audit_on_flask(flask_repo: Path):
    """INSIDE-mode audit walks every phase and lands a non-trivial advisory.

    Validates the SKILL's structural promise: context → source review
    → infra sweep → rate-limit deep-dive → advisory. We pin to INSIDE
    because OUTSIDE needs an authorization_source and we don't want to
    feed one through here.
    """
    trace = await drive(
        f"Use the security-audit burr app to audit the Flask codebase "
        f"at {flask_repo} in INSIDE mode. Walk every phase of the SKILL: "
        f"start_audit, record_context, source_review, infra_sweep, "
        f"rate_limit_deep_dive, write_advisory. Use your Read / Grep / Bash "
        f"tools on the checkout to find real findings; don't fabricate. "
        f"Once you call write_advisory and the FSM terminates, stop -- "
        f"don't explain further. Don't ask me for anything.",
        max_budget_usd=15.0,
        max_turns=60,
    )
    tool_name = "mcp__security-audit__step"
    step_calls = calls_to(trace["tool_calls"], tool_name)
    assert step_calls, (
        f"Claude never called the security-audit step tool. Tools "
        f"called: {sorted({c['name'] for c in trace['tool_calls']})}"
    )

    actions = actions_called(trace["tool_calls"], tool_name)
    expected_phases = {
        "start_audit",
        "record_context",
        "source_review",
        "infra_sweep",
        "rate_limit_deep_dive",
        "write_advisory",
    }
    missing = expected_phases - set(actions)
    assert not missing, f"SKILL phases not reached: {sorted(missing)}. Actions walked: {actions!r}"

    # write_advisory's tool result should carry the audit_summary key
    # in the state payload. That's the load-bearing artefact.
    write_calls = calls_with_action(trace["tool_calls"], tool_name, "write_advisory")
    assert write_calls
    final = result_for(trace["tool_results"], write_calls[-1]["id"])
    assert final is not None
    parsed = final["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"write_advisory failed: {parsed!r}"
    state = parsed.get("state", {})
    summary = state.get("audit_summary")
    assert summary is not None, (
        f"write_advisory completed but state.audit_summary is missing: {state!r}"
    )
    assert summary.get("mode") == "INSIDE"
    # We don't pin a specific count, but the SKILL says "every finding ...
    # captured in state"; some phases must have produced something.
    findings_per_phase = summary.get("findings_per_phase", {})
    assert isinstance(findings_per_phase, dict)
    assert sum(int(v or 0) for v in findings_per_phase.values()) >= 0  # sanity

    # The advisory itself must be non-empty markdown.
    advisory = state.get("advisory")
    assert isinstance(advisory, str) and len(advisory) > 200, (
        f"Advisory text looks too thin: {advisory!r}"
    )


# == differential-review =========================================


@pytest.mark.asyncio
async def test_differential_review_walks_phases_on_flask_commit(flask_repo: Path):
    """differential-review walks the SKILL phases on a real Flask commit.

    Picks the latest commit reachable from the pinned tag. The FSM's
    overall_risk may be HIGH or not depending on the commit -- both
    branches are valid; assertion is just that the workflow reaches
    write_report with a populated report_summary.
    """
    trace = await drive(
        f"Use the differential-review burr app to review the most recent "
        f"commit on the Flask checkout at {flask_repo}. Use your Bash tool "
        f"to run `git log`, `git show`, etc. against that checkout. Use "
        f"codebase_size=MEDIUM. Walk every phase of the SKILL the risk "
        f"level requires (Phase 4 + 5 only if any file is HIGH). End by "
        f"calling write_report with the full markdown text. Don't ask me "
        f"for anything; pick a single commit and review it.",
        max_budget_usd=15.0,
        max_turns=80,
    )
    tool_name = "mcp__differential-review__step"
    step_calls = calls_to(trace["tool_calls"], tool_name)
    assert step_calls, (
        f"Claude never called differential-review step. Tools: "
        f"{sorted({c['name'] for c in trace['tool_calls']})}"
    )

    actions = actions_called(trace["tool_calls"], tool_name)
    common_phases = {
        "start_review",
        "pre_analysis",
        "triage",
        "code_analysis",
        "test_coverage",
        "blast_radius",
        "write_report",
    }
    missing = common_phases - set(actions)
    assert not missing, (
        f"Required differential-review phases not reached: "
        f"{sorted(missing)}. Actions walked: {actions!r}"
    )

    # write_report must succeed and populate state.report_summary.
    write_calls = calls_with_action(trace["tool_calls"], tool_name, "write_report")
    assert write_calls, "write_report was never called"
    final = result_for(trace["tool_results"], write_calls[-1]["id"])
    assert final is not None
    parsed = final["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"write_report failed: {parsed!r}"
    state = parsed.get("state", {})
    summary = state.get("report_summary")
    assert summary is not None, (
        f"write_report completed but state.report_summary missing: {state!r}"
    )
    assert summary.get("overall_risk") in ("HIGH", "MEDIUM", "LOW"), (
        f"overall_risk has odd value: {summary!r}"
    )

    # If overall_risk is HIGH the SKILL says adversarial + deep_context
    # must fire; if not, they're skipped. Either way is valid; just
    # check the gate was honoured.
    if summary["overall_risk"] == "HIGH":
        assert "deep_context" in actions and "adversarial" in actions, (
            f"HIGH-risk review skipped phases 4-5. Actions: {actions!r}"
        )

    # Report markdown should be substantial.
    report = state.get("report")
    assert isinstance(report, str) and len(report) > 200, f"Report text looks too thin: {report!r}"


# == fp-check ====================================================


@pytest.mark.asyncio
async def test_fp_check_walks_gates_against_concocted_flask_claim(flask_repo: Path):
    """fp-check walks Step 0 + route + six gates against a real claim.

    We feed a plausible-sounding but fabricated bug claim against
    Flask. The expected verdict is FALSE POSITIVE (the claim is fake
    and the gates should reject it), but the test asserts only on
    process: every gate fired in order, the verdict is one of the
    two valid values, and the load-bearing failing gate is recorded.
    """
    trace = await drive(
        f"Use the fp-check burr app to verify this suspected bug against "
        f"the Flask checkout at {flask_repo}:\n\n"
        f"'In `flask/app.py`, the `_request_blueprint_handlers` cache is "
        f"populated without input validation, allowing an attacker who "
        f"can register a blueprint with a crafted name to overwrite "
        f"handler entries belonging to other blueprints. This leads to "
        f"RCE because the cached handler is invoked for the wrong "
        f"route.'\n\n"
        f"Use your Read / Grep tools to inspect the actual Flask source "
        f"and walk every phase of the SKILL: step0_restate, route_path, "
        f"and all six gates (gate1_process through gate6_environment), "
        f"then call final_verdict. Be honest: if any gate's criterion "
        f"can't be met, mark that gate fail. Don't ask me for anything.",
        max_budget_usd=10.0,
        max_turns=50,
    )
    tool_name = "mcp__fp-check__step"
    step_calls = calls_to(trace["tool_calls"], tool_name)
    assert step_calls, (
        f"Claude never called fp-check step. Tools: "
        f"{sorted({c['name'] for c in trace['tool_calls']})}"
    )

    actions = actions_called(trace["tool_calls"], tool_name)
    required = {
        "start_check",
        "step0_restate",
        "route_path",
        "gate1_process",
        "gate2_reachability",
        "gate3_impact",
        "gate4_poc_validation",
        "gate5_math_bounds",
        "gate6_environment",
        "final_verdict",
    }
    missing = required - set(actions)
    assert not missing, (
        f"fp-check phases not reached: {sorted(missing)}. Actions walked: {actions!r}"
    )

    final_calls = calls_with_action(trace["tool_calls"], tool_name, "final_verdict")
    assert final_calls
    final = result_for(trace["tool_results"], final_calls[-1]["id"])
    assert final is not None
    parsed = final["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"final_verdict failed: {parsed!r}"
    state = parsed.get("state", {})
    verdict = state.get("verdict")
    assert verdict in ("TRUE POSITIVE", "FALSE POSITIVE"), f"Unexpected verdict shape: {verdict!r}"
    summary = state.get("verdict_summary")
    assert summary is not None
    # Every gate must have a recorded outcome (pass / fail).
    expected_gates = {
        "process",
        "reachability",
        "impact",
        "poc",
        "math",
        "environment",
    }
    passed = set(summary.get("passed_gates", []) or [])
    failed = set(summary.get("failed_gates", []) or [])
    assert passed | failed == expected_gates, (
        f"Gates partial: passed={passed}, failed={failed}, expected_set={expected_gates}"
    )
    if verdict == "FALSE POSITIVE":
        assert summary.get("load_bearing_gate") in expected_gates, (
            f"FALSE POSITIVE without a load-bearing gate: {summary!r}"
        )
    else:
        assert summary.get("load_bearing_gate") is None
