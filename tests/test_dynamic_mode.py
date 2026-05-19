"""DYNAMIC mode: per-action tools, visibility tracks current state.

After each step, FastMCP's enable/disable tags hide the tools that
aren't currently reachable. Clients that honour
``notifications/tools/list_changed`` get the freshest list; clients
that don't will see a stale list but still get an
``invalid_transition`` error from the server if they try a stale action.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from burr_mcp import ServingMode
from coffee_order import build_server


@pytest.mark.asyncio
async def test_initial_visibility_is_entrypoint_only():
    server = build_server(ServingMode.DYNAMIC)
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        # Only take_order is the valid entrypoint; pay/fulfill are hidden.
        assert "take_order" in names
        assert "pay" not in names
        assert "fulfill" not in names


@pytest.mark.asyncio
async def test_visibility_shifts_after_step():
    server = build_server(ServingMode.DYNAMIC)
    async with Client(server) as client:
        await client.call_tool("take_order", {"item": "latte"})
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "pay" in names
        assert "take_order" not in names
        assert "fulfill" not in names


@pytest.mark.asyncio
async def test_dynamic_refuses_invalid_transition_too():
    """Even if a stale client tries a hidden action, the server says no."""
    server = build_server(ServingMode.DYNAMIC)
    async with Client(server) as client:
        # take_order is the only visible tool; pay is hidden but we can
        # still attempt to call it. FastMCP routes hidden tools through
        # the same handler, which refuses because pay isn't
        # in valid_next_actions.
        try:
            r = await client.call_tool("pay", {"amount": 5.0})
            out = json.loads(r.content[0].text)
            # If FastMCP let the call through, our handler refused.
            assert out["error"] == "invalid_transition"
        except Exception as e:
            # FastMCP may also reject at the routing layer because the
            # tool is disabled. Either outcome is acceptable.
            assert "pay" in str(e).lower() or "disabled" in str(e).lower() or "not found" in str(e).lower()
