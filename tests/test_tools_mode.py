"""TOOLS mode: every action exposed as its own MCP tool, no gating.

This is the closest analogue to a flat MCP server today. State is
mutated freely by whichever action the client picks.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from burr_mcp import ServingMode
from coffee_order import build_server


@pytest.mark.asyncio
async def test_each_action_registered_as_its_own_tool():
    server = build_server(ServingMode.TOOLS)
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert {"take_order", "pay", "fulfill"} <= names


@pytest.mark.asyncio
async def test_can_call_pay_before_take_order_in_tools_mode():
    """In tools mode there's no transition enforcement.

    Calling ``pay`` first should succeed and mutate state, even though
    the graph says it shouldn't be reachable yet. The whole point of
    tools mode: graph is advisory.
    """
    server = build_server(ServingMode.TOOLS)
    async with Client(server) as client:
        r = await client.call_tool("pay", {"amount": 5.0})
        out = json.loads(r.content[0].text)
        assert out["action"] == "pay"
        assert out["state"]["paid_amount"] == 5.0


@pytest.mark.asyncio
async def test_tools_mode_state_persists_across_calls():
    server = build_server(ServingMode.TOOLS)
    async with Client(server) as client:
        await client.call_tool("take_order", {"item": "latte", "qty": 2})
        await client.call_tool("pay", {"amount": 9.0})
        result = await client.read_resource("burr://state")
        state = json.loads(result[0].text)
        assert state["stage"] == "paid"
        assert state["paid_amount"] == 9.0
        assert state["item"] == "latte"
