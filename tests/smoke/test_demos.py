"""Fast smoke tests: drive a real Claude session through small surface checks.

These verify the wire works: the LLM finds the right MCP tool, refusals
come back structured, the model self-corrects. They do NOT exercise the
SKILL workflows end-to-end against a real codebase; see
``test_skill_workflows.py`` for that.

Run explicitly:

    uv run pytest -m smoke tests/smoke/test_demos.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

from ._helpers import (  # noqa: E402
    actions_called,
    calls_to,
    calls_with_action,
    check_environment_or_skip,
    drive,
    result_for,
)

check_environment_or_skip()


@pytest.mark.asyncio
async def test_coffee_refuses_pay_before_order():
    """The simplest contract: invalid_transition refusal + self-correct."""
    trace = await drive(
        "Use the coffee burr app to place an order, but first try paying "
        "before ordering so we can see the refusal. Then complete the full "
        "order (take_order=latte qty=1, pay $5, fulfill). Don't ask me for "
        "more details, just walk it.",
        max_budget_usd=5.0,
    )
    coffee_calls = calls_to(trace["tool_calls"], "mcp__coffee__step")
    assert coffee_calls, (
        f"Claude never called mcp__coffee__step. Tools called: "
        f"{[c['name'] for c in trace['tool_calls']]}"
    )

    first_action = coffee_calls[0]["input"].get("action")
    assert first_action == "pay", (
        f"Expected first action to be `pay`; got {first_action!r}. "
        f"All actions: {actions_called(trace['tool_calls'], 'mcp__coffee__step')}"
    )

    first_result = result_for(trace["tool_results"], coffee_calls[0]["id"])
    assert first_result is not None
    parsed = first_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") == "invalid_transition", (
        f"Expected invalid_transition refusal; got {parsed!r}"
    )
    assert "take_order" in parsed.get("valid_next_actions", [])

    later_actions = actions_called(trace["tool_calls"], "mcp__coffee__step")[1:]
    assert "take_order" in later_actions, (
        f"Model did not self-correct. Subsequent actions: {later_actions!r}"
    )


@pytest.mark.asyncio
async def test_skill_security_audit_refuses_outside_without_authorization():
    """OUTSIDE/BOTH modes require authorization_source; refusal must be
    structured with the SKILL's wording."""
    trace = await drive(
        "Use the skill-security-audit burr app to audit https://example.com "
        "in OUTSIDE mode, with NO authorization_source set (we want to see "
        "the SKILL's authorization gate fire). Try the start_audit call once; "
        "don't retry or work around it.",
        max_budget_usd=3.0,
        max_turns=15,
    )
    start_calls = calls_with_action(
        trace["tool_calls"], "mcp__skill-security-audit__step", "start_audit"
    )
    assert start_calls, (
        f"Claude never called start_audit on skill-security-audit. "
        f"Tools: {[c['name'] for c in trace['tool_calls']]}"
    )
    first = result_for(trace["tool_results"], start_calls[0]["id"])
    assert first is not None
    parsed = first["parsed"]
    assert parsed is not None
    err = parsed.get("error")
    assert err in ("action_error", "validation_failed"), (
        f"Expected start_audit refusal; got {parsed!r}"
    )
    message = (
        parsed.get("error_message", "") + parsed.get("reason", "") + parsed.get("message", "")
    ).lower()
    assert "authorization" in message


@pytest.mark.asyncio
async def test_incident_response_walks_through_multi_phase_workflow():
    """End-to-end walk through the incident-response FSM."""
    trace = await drive(
        "Use the incident-response burr app. Open a P2 incident with "
        "title 'db latency spiking on shard 7' and reporter alice. "
        "Acknowledge as bob. Run an investigation. Mitigate by rolling "
        "back deploy 89a3. Verify. Resolve. Write a one-paragraph "
        "postmortem. Walk the full FSM; don't ask me for more details.",
        max_budget_usd=10.0,
        max_turns=40,
    )
    step_calls = calls_to(trace["tool_calls"], "mcp__incident-response__step")
    assert len(step_calls) >= 5, (
        f"Expected >=5 phases walked; got {len(step_calls)}. "
        f"Actions: {actions_called(trace['tool_calls'], 'mcp__incident-response__step')}"
    )
    last_result = result_for(trace["tool_results"], step_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"Last incident-response call returned error: {parsed!r}"
    distinct_actions = {c["input"].get("action") for c in step_calls if c["input"].get("action")}
    assert len(distinct_actions) >= 4, f"Expected >=4 distinct actions; got {distinct_actions!r}"
