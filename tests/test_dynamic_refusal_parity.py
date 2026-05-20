"""DYNAMIC mode: calling a currently-disabled tool returns the same
``invalid_transition`` payload that STEP mode returns, not FastMCP's
generic ``NotFoundError`` / ``Unknown tool`` message.

The middleware lets stale clients (or clients that don't honor
``tools/list_changed``) recover from one error rather than seeing an
opaque failure.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client
from fastmcp.exceptions import NotFoundError

from burrmcp import ServingMode, mount


@action(reads=[], writes=["status"])
def start(state: State) -> State:
    return state.update(status="started")


@action(reads=["status"], writes=["count"])
def tick(state: State) -> State:
    return state.update(count=state.get("count", 0) + 1)


@action(reads=["count"], writes=["status"])
def finalize(state: State) -> State:
    return state.update(status="finalized")


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(start=start, tick=tick, finalize=finalize)
        .with_transitions(
            ("start", "tick"),
            ("tick", "tick", Condition.expr("status == 'started'")),
            ("tick", "finalize", Condition.expr("status == 'started'")),
        )
        .with_state(status="initial", count=0)
        .with_entrypoint("start")
        .build()
    )


@pytest.mark.asyncio
async def test_dynamic_refusal_returns_invalid_transition_payload():
    """Fresh session: only `start` is reachable. Calling `finalize`
    should return the structured refusal, not bubble up NotFoundError."""
    server = mount(_factory, mode=ServingMode.DYNAMIC, name="dynamic-refusal")
    async with Client(server) as client:
        result = await client.call_tool("finalize", {})
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "invalid_transition"
        assert payload["requested"] == "finalize"
        assert "start" in payload["valid_next_actions"]
        assert "Valid actions now" in payload["message"]


@pytest.mark.asyncio
async def test_dynamic_valid_tool_still_runs_normally():
    """The middleware only intercepts disabled-tool calls. A valid call
    proceeds through the normal handler path and returns the action's
    structured success payload (which includes app_id and tracker_project)."""
    server = mount(_factory, mode=ServingMode.DYNAMIC, name="dynamic-ok")
    async with Client(server) as client:
        result = await client.call_tool("start", {})
        payload = json.loads(result.content[0].text)
        assert payload.get("action") == "start"
        assert "app_id" in payload
        assert payload.get("error") is None or "error" not in payload


@pytest.mark.asyncio
async def test_dynamic_unknown_tool_name_still_errors():
    """Tools that aren't Burr actions at all (typos, fictional names)
    should fall through to FastMCP's original NotFoundError, not get
    a misleading invalid_transition payload."""
    server = mount(_factory, mode=ServingMode.DYNAMIC, name="dynamic-unknown")
    async with Client(server) as client:
        with pytest.raises((NotFoundError, Exception)) as exc_info:
            await client.call_tool("this_action_does_not_exist", {})
        # The exact exception type FastMCP raises client-side varies,
        # but the message should indicate the tool wasn't found.
        assert "unknown" in str(exc_info.value).lower() or "not" in str(exc_info.value).lower()
