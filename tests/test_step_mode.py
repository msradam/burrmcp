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

from theodosia import ServingMode


@pytest.mark.asyncio
async def test_step_happy_path_three_actions():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # take_order
        r1 = await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out1 = r1.structured_content
        assert out1["action"] == "take_order"
        assert out1["state"]["stage"] == "ordered"
        assert out1["state"]["item"] == "latte"
        # Post-take_order the FSM exposes pay (linear path), add_modifier
        # (loop), and cancel (escape) as the legal next moves.
        assert set(out1["valid_next_actions"]) == {"pay", "add_modifier", "cancel"}

        # pay
        r2 = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        out2 = r2.structured_content
        assert out2["state"]["stage"] == "paid"
        assert out2["valid_next_actions"] == ["fulfill"]

        # fulfill (terminal)
        r3 = await client.call_tool("step", {"action": "fulfill", "inputs": {}})
        out3 = r3.structured_content
        assert out3["state"]["stage"] == "fulfilled"
        assert out3["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_step_refuses_invalid_transition():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # Try to pay before taking an order.
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"
        assert out["requested"] == "pay"
        assert out["valid_next_actions"] == ["take_order"]


@pytest.mark.asyncio
async def test_step_refuses_unknown_action():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "nonexistent", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "unknown_action"
        assert "take_order" in out["known_actions"]


@pytest.mark.asyncio
async def test_unknown_action_steers_like_invalid_transition():
    # A hallucinated action name is the refusal a weaker model hits most.
    # It must carry the same steering fields an invalid_transition does
    # from the same state, so the model can recover from the response alone.
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        unknown = (
            await client.call_tool("step", {"action": "definitely_not_an_action"})
        ).structured_content
        invalid = (
            await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        ).structured_content

    assert unknown["error"] == "unknown_action"
    assert invalid["error"] == "invalid_transition"
    # Same reachable set from the same (entry) state.
    assert unknown["valid_next_actions"] == invalid["valid_next_actions"] == ["take_order"]
    # Full steering surface present, not just known_actions.
    assert "take_order" in unknown["known_actions"]
    assert unknown["message"]
    assert unknown["next_hint"]
    assert "take_order" in unknown["next_hint"]


@pytest.mark.asyncio
async def test_state_resource_reflects_progress():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "americano", "qty": 1}}
        )
        result = await client.read_resource("theodosia://state")
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
        result = await client.read_resource("theodosia://next")
        assert json.loads(result[0].text) == ["take_order"]
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        result = await client.read_resource("theodosia://next")
        assert set(json.loads(result[0].text)) == {"pay", "add_modifier", "cancel"}
