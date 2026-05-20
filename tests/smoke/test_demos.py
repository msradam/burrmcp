"""Smoke tests: drive a real Claude via the Agent SDK against the demo bench.

These are deselected by default. Run explicitly:

    uv run pytest -m smoke tests/smoke/

What they cover that the unit suite cannot
-----------------------------------------

The shipped 490-test suite exercises the Burr FSMs and the BurrMCP
adapter directly. These smoke tests instead drive an actual Claude
session through the Agent SDK, with the demo bench's ``.mcp.json``
wired in, to verify that the LLM driving the FSMs still behaves
correctly: that it calls the right MCP tool, that it self-corrects on
refusals, and that the SKILL workflows still walk in the right order
when the LLM (not test code) is choosing actions.

This catches "the prompt template changed and now the LLM walks the
SKILL wrong" regressions that pytest can't see.

Prerequisites
-------------

* `claude` CLI on PATH and logged in (`claude login`); the Agent SDK
  inherits this OAuth session. Without a credentials file, every
  test in this module is converted to a skip with a clear message
  instead of failing opaquely.
* `~/burr-mcp-demo/.mcp.json` present and pointing at this repo.
* `claude-agent-sdk` Python dep installed (dev dependency).

Billing
-------

When the operator is logged into a Claude subscription that includes
Agent SDK credit (Max 20x at the time of writing), all calls in this
file bill against that credit, not per-token via an API key. Each
test sets a tight ``max_budget_usd`` + ``max_turns`` as a safety
floor in case the credit is exhausted and the SDK silently falls
back.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.smoke

_MCP_CONFIG = Path("~/burr-mcp-demo/.mcp.json").expanduser()

if shutil.which("claude") is None or not _MCP_CONFIG.exists():
    pytest.skip(
        "Smoke tests require the `claude` CLI on PATH plus "
        "~/burr-mcp-demo/.mcp.json. Run `claude login` first.",
        allow_module_level=True,
    )

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )
except ImportError:  # pragma: no cover - covered by the dep being present
    pytest.skip("claude-agent-sdk not installed", allow_module_level=True)


# The Agent SDK reads OAuth credentials from ~/.claude/.credentials.json
# when running against a Claude subscription. If that file is missing the
# subprocess exits fast with an opaque "error result: success" message;
# skip cleanly so the user sees a "run claude login" hint instead.
_CREDENTIALS_FILE = Path("~/.claude/.credentials.json").expanduser()
if not _CREDENTIALS_FILE.exists():
    pytest.skip(
        "Smoke tests require Claude OAuth credentials. Run `claude login` "
        f"once, then re-run `pytest -m smoke`. Looked for: {_CREDENTIALS_FILE}",
        allow_module_level=True,
    )


# == helpers =====================================================


async def _drive(
    prompt: str,
    *,
    max_budget_usd: float = 0.25,
    max_turns: int = 20,
) -> dict[str, Any]:
    """Send `prompt` to Claude with the demo bench wired in, collect the trace.

    Returns a dict with:
      - tool_calls: list of {"id", "name", "input"} for every tool the
        model invoked
      - tool_results: list of {"tool_use_id", "content", "is_error",
        "parsed"} where parsed is the JSON payload BurrMCP returned
        (parsed via _parse_tool_result)
      - final_text: concatenated text from assistant messages
      - result: the ResultMessage (cost, error info, etc.)
    """
    options = ClaudeAgentOptions(
        mcp_servers=_MCP_CONFIG,
        allowed_tools=["mcp__*"],
        permission_mode="bypassPermissions",
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
    )

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    final_text_parts: list[str] = []
    result_message: ResultMessage | None = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {"id": block.id, "name": block.name, "input": dict(block.input)}
                    )
                elif isinstance(block, TextBlock):
                    final_text_parts.append(block.text)
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        tool_results.append(
                            {
                                "tool_use_id": block.tool_use_id,
                                "content": block.content,
                                "is_error": block.is_error,
                                "parsed": _parse_tool_result(block.content),
                            }
                        )
        elif isinstance(msg, ResultMessage):
            result_message = msg

    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_text": "\n".join(final_text_parts),
        "result": result_message,
    }


def _parse_tool_result(content: Any) -> dict | None:
    """Extract the JSON payload from a ToolResultBlock.content.

    BurrMCP returns structured dicts; FastMCP wraps them as MCP content
    blocks (a list of text blocks whose `text` is the JSON serialised
    response). Try every shape we might see.
    """
    if content is None:
        return None
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                break
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def _calls_to(tool_calls: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
    return [c for c in tool_calls if c["name"] == tool_name]


def _result_for(tool_results: list[dict[str, Any]], tool_use_id: str) -> dict[str, Any] | None:
    for r in tool_results:
        if r["tool_use_id"] == tool_use_id:
            return r
    return None


# == cases =======================================================


@pytest.mark.asyncio
async def test_coffee_refuses_pay_before_order():
    """The simplest contract: invalid_transition refusal + self-correct.

    A working FSM-as-API + working MCP wiring produces:
    1. The model calls `mcp__coffee__step` with action=pay first
    2. The server returns {"error": "invalid_transition",
       "valid_next_actions": ["take_order"]}
    3. The model self-corrects: take_order, then pay, then fulfill (or
       at least take_order, then pay)
    """
    trace = await _drive(
        "Use the coffee burr app to place an order, but first try paying "
        "before ordering so we can see the refusal. Then complete the full "
        "order (take_order=latte qty=1, pay $5, fulfill). Don't ask me for "
        "more details, just walk it.",
    )
    coffee_calls = _calls_to(trace["tool_calls"], "mcp__coffee__step")
    assert coffee_calls, (
        f"Claude never called mcp__coffee__step. Tools called: "
        f"{[c['name'] for c in trace['tool_calls']]}"
    )

    # First call should be the deliberate-refusal pay-before-order.
    first_action = coffee_calls[0]["input"].get("action")
    assert first_action == "pay", (
        f"Expected first action to be `pay` (the refusal case); got "
        f"{first_action!r}. All actions: "
        f"{[c['input'].get('action') for c in coffee_calls]}"
    )

    # That first call's tool result should be the structured refusal.
    first_result = _result_for(trace["tool_results"], coffee_calls[0]["id"])
    assert first_result is not None, "No tool_result captured for the pay-first call"
    parsed = first_result["parsed"]
    assert parsed is not None, f"Tool result wasn't JSON: {first_result['content']!r}"
    assert parsed.get("error") == "invalid_transition", (
        f"Expected refusal payload with error=invalid_transition; got {parsed!r}"
    )
    assert "take_order" in parsed.get("valid_next_actions", []), (
        f"Refusal payload should list take_order as valid_next; got "
        f"{parsed.get('valid_next_actions')!r}"
    )

    # And the model must self-correct: a take_order call appears after
    # the failed pay.
    later_actions = [c["input"].get("action") for c in coffee_calls[1:]]
    assert "take_order" in later_actions, (
        f"Model did not self-correct to take_order after the refusal. "
        f"Subsequent actions: {later_actions!r}"
    )


@pytest.mark.asyncio
async def test_skill_security_audit_refuses_outside_without_authorization():
    """The skill-security-audit FSM's start_audit refuses OUTSIDE/BOTH
    modes without an authorization_source; the SKILL says you need
    written authorization before probing production from outside. We
    drive Claude in a way that should trip that refusal."""
    trace = await _drive(
        "Use the skill-security-audit burr app to audit https://example.com "
        "in OUTSIDE mode, with NO authorization_source set (we want to see "
        "the SKILL's authorization gate fire). Just try the start_audit "
        "call once; don't retry or work around it.",
        max_budget_usd=0.25,
        max_turns=15,
    )
    audit_calls = _calls_to(trace["tool_calls"], "mcp__skill-security-audit__step")
    assert audit_calls, (
        f"Claude never called the skill-security-audit step tool. Tools "
        f"called: {[c['name'] for c in trace['tool_calls']]}"
    )
    start_calls = [c for c in audit_calls if c["input"].get("action") == "start_audit"]
    assert start_calls, "Claude never called start_audit on the skill-security-audit app"
    first_start_result = _result_for(trace["tool_results"], start_calls[0]["id"])
    assert first_start_result is not None
    parsed = first_start_result["parsed"]
    assert parsed is not None, (
        f"start_audit tool result wasn't JSON: {first_start_result['content']!r}"
    )
    # The action body raises ValueError when authorization_source is missing
    # for OUTSIDE/BOTH. The adapter surfaces that as action_error in MCP.
    # We also accept validation_failed in case the action's validation
    # path catches it. Either way, the model should see a refusal.
    err = parsed.get("error")
    assert err in ("action_error", "validation_failed"), (
        f"Expected start_audit to refuse with action_error or validation_failed; got {parsed!r}"
    )
    message_text = (
        parsed.get("error_message", "") + parsed.get("reason", "") + parsed.get("message", "")
    ).lower()
    assert "authorization" in message_text, (
        f"Refusal message should mention authorization; got {parsed!r}"
    )


@pytest.mark.asyncio
async def test_incident_response_walks_through_multi_phase_workflow():
    """A multi-step walk: the model should call the incident-response
    step tool multiple times, advance through the gated phases, and
    end with a populated state.summary or a terminal status."""
    trace = await _drive(
        "Use the incident-response burr app. Open a P2 incident with "
        "title 'db latency spiking on shard 7' and reporter alice. "
        "Acknowledge as bob. Run an investigation. Mitigate by rolling "
        "back deploy 89a3. Verify. Resolve. Write a one-paragraph "
        "postmortem. Walk the full FSM; don't ask me for more details.",
        max_budget_usd=0.50,
        max_turns=30,
    )
    incident_calls = _calls_to(trace["tool_calls"], "mcp__incident-response__step")
    assert len(incident_calls) >= 5, (
        f"Expected the model to walk at least 5 phases of the "
        f"incident-response FSM; got {len(incident_calls)} step calls. "
        f"Actions: {[c['input'].get('action') for c in incident_calls]}"
    )
    # No invalid_transition errors on the final-state path: the model
    # might hit some refusals mid-stream and self-correct, but the LAST
    # incident-response call should succeed (not be a refusal).
    last_result = _result_for(trace["tool_results"], incident_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, (
        f"Last incident-response step call returned an error: {parsed!r}. "
        f"The model didn't complete the walk cleanly."
    )
    # And the model must have called more than one distinct action; if
    # it only called `report` 10 times that's a different kind of broken.
    distinct_actions = {
        c["input"].get("action") for c in incident_calls if c["input"].get("action")
    }
    assert len(distinct_actions) >= 4, (
        f"Expected the model to use at least 4 distinct actions; got {distinct_actions!r}"
    )
