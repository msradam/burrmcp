"""burr://history captures every action attempt in a session.

The history resource records successful steps and refusals alike,
keyed by ``ctx.session_id``. In factory mode each session sees only
its own history; in shared-app mode each session sees the timeline
of its own calls (state may have been mutated by other sessions in
between, but the history records what *this* session did).
"""

from __future__ import annotations

import json

import pytest
from coffee_order import build_application, build_server
from fastmcp import Client

from burrmcp import ServingMode, mount


@pytest.mark.asyncio
async def test_history_records_three_successful_steps():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 9.0}})
        await client.call_tool("step", {"action": "fulfill", "inputs": {}})

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 3
        assert [h["action"] for h in history] == ["take_order", "pay", "fulfill"]
        assert [h["seq"] for h in history] == [0, 1, 2]
        assert all(h["refused"] is False for h in history)
        assert history[0]["state_after"]["item"] == "latte"
        assert history[2]["valid_next_actions"] == []  # terminal


@pytest.mark.asyncio
async def test_history_records_refusals_with_reason():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # Refusal #1: invalid transition (pay before take_order).
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        # Refusal #2: unknown action.
        await client.call_tool("step", {"action": "nonexistent", "inputs": {}})
        # Successful step.
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "mocha"}})

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 3
        assert history[0]["refused"] is True
        assert history[0]["refusal_reason"] == "invalid_transition"
        assert history[0]["state_after"] is None
        assert history[0]["valid_next_actions"] == ["take_order"]
        assert history[1]["refused"] is True
        assert history[1]["refusal_reason"] == "unknown_action"
        assert history[2]["refused"] is False
        assert history[2]["state_after"]["item"] == "mocha"


@pytest.mark.asyncio
async def test_history_per_session_in_factory_mode():
    """Two clients on a factory-mounted server have independent histories."""
    server = mount(build_application, mode=ServingMode.STEP, name="coffee-iso")

    async with Client(server) as client_a:
        await client_a.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})

        async with Client(server) as client_b:
            await client_b.call_tool(
                "step", {"action": "take_order", "inputs": {"item": "americano"}}
            )
            history_b = json.loads((await client_b.read_resource("burr://history"))[0].text)
            assert len(history_b) == 1
            assert history_b[0]["inputs"]["item"] == "americano"

        history_a = json.loads((await client_a.read_resource("burr://history"))[0].text)
        assert len(history_a) == 1
        assert history_a[0]["inputs"]["item"] == "latte"


@pytest.mark.asyncio
async def test_history_records_in_tools_mode():
    """TOOLS mode records each per-action tool call."""
    server = build_server(ServingMode.TOOLS)
    async with Client(server) as client:
        await client.call_tool("take_order", {"item": "latte", "qty": 1})
        await client.call_tool("pay", {"amount": 5.0})

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert [h["action"] for h in history] == ["take_order", "pay"]
        assert all(h["refused"] is False for h in history)


@pytest.mark.asyncio
async def test_history_records_in_dynamic_mode():
    """DYNAMIC mode records per-action successes."""
    server = build_server(ServingMode.DYNAMIC)
    async with Client(server) as client:
        await client.call_tool("take_order", {"item": "espresso", "qty": 1})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1
        assert history[0]["action"] == "take_order"


@pytest.mark.asyncio
async def test_history_entry_shape_is_complete():
    """A history entry has all documented fields."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        entry = history[0]
        assert set(entry.keys()) >= {
            "seq",
            "ts",
            "action",
            "inputs",
            "state_after",
            "valid_next_actions",
            "refused",
            "refusal_reason",
        }
