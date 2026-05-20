"""DYNAMIC mode: per-session visibility under concurrent clients.

These tests probe whether two concurrent MCP sessions on a DYNAMIC-mode
server see independent tool visibility. FastMCP 3.2's
``enable()``/``disable()`` transforms are documented as per-session,
which is the contract this test exercises against a factory-mounted
server. If FastMCP's implementation ever regresses on that, these
tests will catch it.
"""

from __future__ import annotations

import json

import pytest
from coffee_order import build_application
from fastmcp import Client

from burrmcp import ServingMode, mount


@pytest.mark.asyncio
async def test_two_concurrent_sessions_see_independent_visibility():
    server = mount(build_application, mode=ServingMode.DYNAMIC, name="coffee-dyn")

    async with Client(server) as client_a:
        # Both sessions start at entrypoint: take_order visible, pay/fulfill hidden.
        tools_a_initial = {t.name for t in await client_a.list_tools()}
        assert "take_order" in tools_a_initial
        assert "pay" not in tools_a_initial
        assert "fulfill" not in tools_a_initial

        # A advances past take_order: pay should now be visible to A.
        await client_a.call_tool("take_order", {"item": "latte"})
        tools_a_after = {t.name for t in await client_a.list_tools()}
        assert "pay" in tools_a_after
        assert "take_order" not in tools_a_after

        # Open session B. B should still be at the entrypoint:
        # take_order visible, pay/fulfill hidden, regardless of A's progress.
        async with Client(server) as client_b:
            tools_b = {t.name for t in await client_b.list_tools()}
            assert "take_order" in tools_b, (
                f"DYNAMIC visibility leaked across sessions: B sees A's state. "
                f"B's tools: {sorted(tools_b)}"
            )
            assert "pay" not in tools_b, (
                f"DYNAMIC visibility leaked across sessions: B sees A's state. "
                f"B's tools: {sorted(tools_b)}"
            )

            # Verify B's state resource is also independent.
            state_b = json.loads((await client_b.read_resource("burr://state"))[0].text)
            assert state_b.get("stage") == "new"
            assert "item" not in state_b


@pytest.mark.asyncio
async def test_visibility_advances_in_each_session_independently():
    """B advancing through its own ticket doesn't disturb A's visibility."""
    server = mount(build_application, mode=ServingMode.DYNAMIC, name="coffee-dyn2")

    async with Client(server) as client_a:
        await client_a.call_tool("take_order", {"item": "latte"})
        # A is now at "pay" stage.

        async with Client(server) as client_b:
            # B walks through its own order start-to-finish.
            await client_b.call_tool("take_order", {"item": "americano"})
            await client_b.call_tool("pay", {"amount": 4.0})
            tools_b_end = {t.name for t in await client_b.list_tools()}
            assert "fulfill" in tools_b_end

        # A's visibility wasn't touched by B's walk.
        tools_a = {t.name for t in await client_a.list_tools()}
        assert "pay" in tools_a
        assert "take_order" not in tools_a
        assert "fulfill" not in tools_a
