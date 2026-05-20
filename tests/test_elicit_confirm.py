"""Tests for examples/elicit_confirm.py.

Drives the FSM through MCP with a fake elicitation handler attached
to the FastMCP test Client. Confirms that:

* ctx.elicit's accept-with-"confirm" path moves staged to purged
* ctx.elicit's accept-with-"abort" path clears staging without purge
* ctx.elicit's decline path also clears without purge
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from elicit_confirm import build_server  # noqa: E402


def _make_handler(answer: str | None, *, decline: bool = False):
    """Build an elicitation handler that returns ``answer`` or declines."""

    async def handler(message, response_type, params, context):
        if decline:
            return ElicitResult(action="decline", content=None)
        return answer

    return handler


@pytest.mark.asyncio
async def test_purge_on_user_confirm():
    server = build_server()
    handler = _make_handler("confirm")
    async with Client(server, elicitation_handler=handler) as client:
        await client.call_tool("step", {"action": "stage", "inputs": {"item": "a.txt"}})
        await client.call_tool("step", {"action": "stage", "inputs": {"item": "b.txt"}})
        r = await client.call_tool("step", {"action": "purge", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out.get("error") is None, out
        assert out["state"]["staged"] == []
        assert out["state"]["purged"] == ["a.txt", "b.txt"]
        assert out["state"]["outcome"] == "purged"


@pytest.mark.asyncio
async def test_abort_on_user_abort():
    server = build_server()
    handler = _make_handler("abort")
    async with Client(server, elicitation_handler=handler) as client:
        await client.call_tool("step", {"action": "stage", "inputs": {"item": "x.txt"}})
        r = await client.call_tool("step", {"action": "purge", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["staged"] == []
        assert out["state"]["purged"] == []
        assert out["state"]["outcome"] == "aborted"


@pytest.mark.asyncio
async def test_abort_on_user_decline():
    """A declined elicitation is treated the same as 'abort'."""
    server = build_server()
    handler = _make_handler(None, decline=True)
    async with Client(server, elicitation_handler=handler) as client:
        await client.call_tool("step", {"action": "stage", "inputs": {"item": "y.txt"}})
        r = await client.call_tool("step", {"action": "purge", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["purged"] == []
        assert out["state"]["outcome"] == "aborted"


@pytest.mark.asyncio
async def test_stage_rejects_empty_item():
    server = build_server()
    handler = _make_handler("confirm")
    async with Client(server, elicitation_handler=handler) as client:
        r = await client.call_tool("step", {"action": "stage", "inputs": {"item": "   "}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert "item must not be empty" in out["error_message"]
