"""Per-action timeout overrides.

A ``ToolSpec.timeout_seconds`` declared at lift time annotates the
wrapped action with a per-call timeout. ``mount`` reads the annotation
back and applies it in preference to its own
``action_timeout_seconds`` default. Annotating a hand-written Burr
action with ``fn._theodosia_timeout_seconds = N`` works too.
"""

from __future__ import annotations

import asyncio

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client, FastMCP

from theodosia import ServingMode, ToolSpec, burr_app_from_fastmcp, mount


@pytest.mark.asyncio
async def test_tool_spec_timeout_wins_over_server_default():
    """A per-tool 0.1s timeout fires even with a server default of 10s."""
    flat = FastMCP("per-tool-timeout")

    @flat.tool
    async def slow_one() -> dict:
        await asyncio.sleep(2.0)
        return {"done": True}

    @flat.tool
    async def fast_one() -> dict:
        await asyncio.sleep(0.01)
        return {"done": True}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="slow_one",
        tool_specs={
            "slow_one": ToolSpec(timeout_seconds=0.1),
            "fast_one": ToolSpec(),
        },
        transitions=[("slow_one", "fast_one")],
    )
    server = mount(
        app,
        mode=ServingMode.STEP,
        name="per-tool",
        action_timeout_seconds=10.0,  # generous server default
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "slow_one", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "action_timeout"
        # The per-tool override, not the server default, is what fired.
        assert out["timeout_seconds"] == 0.1


@pytest.mark.asyncio
async def test_tool_spec_timeout_applies_when_server_default_is_none():
    """A per-tool timeout works even with no server-wide default."""
    flat = FastMCP("override-no-default")

    @flat.tool
    async def slow() -> dict:
        await asyncio.sleep(2.0)
        return {"done": True}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="slow",
        tool_specs={"slow": ToolSpec(timeout_seconds=0.1)},
    )
    server = mount(
        app,
        mode=ServingMode.STEP,
        name="override-no-default",
        # action_timeout_seconds defaults to None.
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "slow", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "action_timeout"
        assert out["timeout_seconds"] == 0.1


@pytest.mark.asyncio
async def test_no_per_tool_override_inherits_server_default():
    """When a tool has no override, the server-wide default applies."""
    flat = FastMCP("inherits")

    @flat.tool
    async def slow() -> dict:
        await asyncio.sleep(2.0)
        return {"done": True}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="slow",
        # No tool_specs override for ``slow``.
        tool_specs={"slow": ToolSpec()},
    )
    server = mount(
        app,
        mode=ServingMode.STEP,
        name="inherits",
        action_timeout_seconds=0.2,
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "slow", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "action_timeout"
        assert out["timeout_seconds"] == 0.2


@pytest.mark.asyncio
async def test_hand_tagged_action_function_works_too():
    """A hand-written @action whose fn carries the magic attribute
    also gets the per-action override."""

    @action(reads=[], writes=["done"])
    async def hand_slow(state: State) -> State:
        await asyncio.sleep(2.0)
        return state.update(done=True)

    # @action returns the underlying function with extra attributes;
    # FunctionBasedAction.fn IS this same function, so setting the
    # magic attribute here is what mount() reads back.
    hand_slow._theodosia_timeout_seconds = 0.1  # type: ignore[attr-defined]

    def app_factory():
        return (
            ApplicationBuilder()
            .with_actions(hand_slow=hand_slow)
            .with_state(done=False)
            .with_entrypoint("hand_slow")
            .build()
        )

    server = mount(
        app_factory,
        mode=ServingMode.STEP,
        name="hand-tagged",
        # No server default; per-action override is the only timeout.
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "hand_slow", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "action_timeout"
        assert out["timeout_seconds"] == 0.1
