"""Smoke test: caller_sample compat probe against Claude Code.

Claude Code's headless CLI does NOT support MCP server-to-client
sampling (``sampling/createMessage``); it returns the explicit error
"Client does not support sampling". The server-side action surfaces
this as an ``action_error`` refusal. This test asserts the documented
compat behavior; if Claude Code adds sampling support, the test will
fail differently and we should rewrite to the happy path.

The happy-path coverage for ctx.sample lives in
``tests/test_caller_sample.py`` via ``fastmcp.Client(sampling_handler=)``.

Run explicitly:

    uv run pytest -m smoke tests/smoke/test_caller_sample.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

from ._helpers import (
    actions_called,
    calls_to,
    check_environment_or_skip,
    drive,
    result_for,
)

check_environment_or_skip()


@pytest.mark.asyncio
async def test_caller_sample_compose_refuses_against_claude_code():
    """ctx.sample raises in Claude Code; server surfaces as action_error."""
    trace = await drive(
        "Use the caller-sample burr app. Call its step tool with "
        "action='compose', inputs={'topic': 'photosynthesis', 'style': 'concise'}. "
        "Then report whatever the tool returned, even if it errored.",
        max_budget_usd=3.0,
        max_turns=6,
    )
    sample_calls = calls_to(trace["tool_calls"], "mcp__caller-sample__step")
    assert sample_calls, (
        f"Claude never called mcp__caller-sample__step. Tools called: "
        f"{[c['name'] for c in trace['tool_calls']]}"
    )
    actions = actions_called(trace["tool_calls"], "mcp__caller-sample__step")
    assert "compose" in actions, f"compose was never called; got {actions}"

    compose_call = next(c for c in sample_calls if c["input"].get("action") == "compose")
    result = result_for(trace["tool_results"], compose_call["id"])
    assert result is not None, "compose tool result not found"
    parsed = result["parsed"]
    assert parsed is not None, f"compose result had no JSON body: {result}"
    assert parsed.get("error") == "action_error", f"Expected action_error refusal; got {parsed}"
    msg = parsed.get("error_message", "")
    assert "sampling" in msg.lower(), f"Expected sampling-related error message; got {msg!r}"
