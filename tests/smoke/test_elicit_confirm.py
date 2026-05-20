"""Smoke test: elicit_confirm compat probe against Claude Code.

Claude Code's headless CLI auto-declines MCP elicitation requests
(no UI prompt in headless mode). The demo's safety-rail logic treats
not-accepted as abort, so the FSM ends at ``outcome="aborted"`` after
a real Claude session walks stage -> purge.

This is the correct defensive behavior: when the client cannot
confirm with the user, default-deny. The test asserts that property.

The happy-path coverage for ctx.elicit (accept vs. abort vs. decline)
lives in ``tests/test_elicit_confirm.py`` via
``fastmcp.Client(elicitation_handler=)``.

Run explicitly:

    uv run pytest -m smoke tests/smoke/test_elicit_confirm.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

from ._helpers import (  # noqa: E402
    actions_called,
    calls_to,
    check_environment_or_skip,
    drive,
    result_for,
)

check_environment_or_skip()


@pytest.mark.asyncio
async def test_elicit_confirm_purge_defaults_to_aborted():
    """Headless Claude Code can't accept the elicit; FSM defaults to abort."""
    trace = await drive(
        "Use the elicit-confirm burr app. Stage one item via "
        "step(action='stage', inputs={'item': 'a.txt'}). Then call "
        "step(action='purge'). Report what each call returned.",
        max_budget_usd=3.0,
        max_turns=8,
    )
    purge_calls = calls_to(trace["tool_calls"], "mcp__elicit-confirm__step")
    assert purge_calls, (
        f"Claude never called the elicit-confirm step tool. "
        f"Tools called: {[c['name'] for c in trace['tool_calls']]}"
    )
    actions = actions_called(trace["tool_calls"], "mcp__elicit-confirm__step")
    assert "stage" in actions and "purge" in actions, f"Expected stage + purge; got {actions}"

    purge_call = next(c for c in purge_calls if c["input"].get("action") == "purge")
    result = result_for(trace["tool_results"], purge_call["id"])
    assert result is not None, "purge tool result not found"
    parsed = result["parsed"]
    assert parsed is not None, f"purge result had no JSON body: {result}"
    assert parsed.get("error") is None, parsed
    state = parsed.get("state", {})
    assert state.get("outcome") == "aborted", (
        f"Expected outcome=aborted (default-deny when client can't elicit); got state={state}"
    )
    assert state.get("purged") == [], f"Nothing should be purged; got {state.get('purged')}"
