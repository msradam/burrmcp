"""STEP mode: one meta-tool, transitions enforced.

Drives the mounted server with an in-process FastMCP Client. No
subprocess, no stdio framing, same trick Circe uses to test its own
in-process MCP server.
"""

from __future__ import annotations

import json

import pytest
from coffee_order import build_server
from fastmcp import Client

from burrmcp import ServingMode


@pytest.mark.asyncio
async def test_step_happy_path_three_actions():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # take_order
        r1 = await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out1 = json.loads(r1.content[0].text)
        assert out1["action"] == "take_order"
        assert out1["state"]["stage"] == "ordered"
        assert out1["state"]["item"] == "latte"
        assert out1["valid_next_actions"] == ["pay"]

        # pay
        r2 = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        out2 = json.loads(r2.content[0].text)
        assert out2["state"]["stage"] == "paid"
        assert out2["valid_next_actions"] == ["fulfill"]

        # fulfill (terminal)
        r3 = await client.call_tool("step", {"action": "fulfill", "inputs": {}})
        out3 = json.loads(r3.content[0].text)
        assert out3["state"]["stage"] == "fulfilled"
        assert out3["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_step_refuses_invalid_transition():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # Try to pay before taking an order.
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["requested"] == "pay"
        assert out["valid_next_actions"] == ["take_order"]


@pytest.mark.asyncio
async def test_step_refuses_unknown_action():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "nonexistent", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "unknown_action"
        assert "take_order" in out["known_actions"]


@pytest.mark.asyncio
async def test_state_resource_reflects_progress():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "americano", "qty": 1}}
        )
        result = await client.read_resource("burr://state")
        state = json.loads(result[0].text)
        assert state["stage"] == "ordered"
        assert state["item"] == "americano"
        # Internal Burr keys must not leak.
        assert "__PRIOR_STEP" not in state
        assert "__SEQUENCE_ID" not in state


@pytest.mark.asyncio
async def test_next_resource_lists_valid_actions():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        result = await client.read_resource("burr://next")
        assert json.loads(result[0].text) == ["take_order"]
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        result = await client.read_resource("burr://next")
        assert json.loads(result[0].text) == ["pay"]
