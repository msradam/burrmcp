"""Per-session isolation: factory mode gives each MCP session its own state.

The coffee server mounted with a factory (instead of a single
Application instance) builds a fresh Application on each session's
first tool call. Two concurrent clients see independent orders.
"""

from __future__ import annotations

import json

import pytest
from coffee_order import build_application
from fastmcp import Client

from burr_mcp import ServingMode, mount


def build_isolated_server(mode: ServingMode = ServingMode.STEP):
    """Mount the coffee example with a factory for per-session state."""
    return mount(build_application, mode=mode, name="coffee-isolated")


@pytest.mark.asyncio
async def test_two_clients_have_independent_state():
    server = build_isolated_server()

    async with Client(server) as client_a:
        await client_a.call_tool(
            "step",
            {"action": "take_order", "inputs": {"item": "latte", "qty": 1}},
        )
        state_a = json.loads((await client_a.read_resource("burr://state"))[0].text)

        async with Client(server) as client_b:
            # Fresh session B: should still be at the entrypoint, no order.
            state_b = json.loads((await client_b.read_resource("burr://state"))[0].text)
            next_b = json.loads((await client_b.read_resource("burr://next"))[0].text)

            assert state_a["stage"] == "ordered"
            assert state_a["item"] == "latte"

            # B is untouched.
            assert "item" not in state_b
            assert next_b == ["take_order"]

            # B places its own different order.
            await client_b.call_tool(
                "step",
                {"action": "take_order", "inputs": {"item": "americano", "qty": 3}},
            )
            state_b_after = json.loads((await client_b.read_resource("burr://state"))[0].text)
            assert state_b_after["item"] == "americano"
            assert state_b_after["qty"] == 3

        # A's state was not disturbed by B's order.
        state_a_again = json.loads((await client_a.read_resource("burr://state"))[0].text)
        assert state_a_again["item"] == "latte"
        assert state_a_again["qty"] == 1


@pytest.mark.asyncio
async def test_shared_mode_still_works():
    """Passing an Application instance (not a factory) shares state.

    This is the original v0.0.1 behavior. Verifies backwards-compat.
    """
    shared_app = build_application()
    server = mount(shared_app, mode=ServingMode.STEP, name="coffee-shared")

    async with Client(server) as client_a:
        await client_a.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )

    async with Client(server) as client_b:
        # Same server, fresh client; the shared Application carries state from A.
        state = json.loads((await client_b.read_resource("burr://state"))[0].text)
        assert state["item"] == "latte"
        assert state["stage"] == "ordered"


@pytest.mark.asyncio
async def test_factory_rejects_non_application_return():
    """Helpful error when the factory returns the wrong type."""
    with pytest.raises(TypeError, match=r"expected a burr\.core\.Application"):
        mount(lambda: "not an application", mode=ServingMode.STEP)


@pytest.mark.asyncio
async def test_mount_rejects_non_application_non_callable():
    with pytest.raises(TypeError, match="Application or a callable"):
        mount(42, mode=ServingMode.STEP)  # type: ignore[arg-type]
