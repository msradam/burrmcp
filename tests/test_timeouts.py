"""Action timeouts: slow actions are cancelled, state doesn't advance,
history records ``action_timeout``.

A stuck action would otherwise hold the per-session lock indefinitely.
The server-wide ``action_timeout_seconds`` cap wraps ``app.astep`` in
``asyncio.wait_for`` so slow async work gets cancelled cleanly.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from burrmcp import ServingMode, mount


@action(reads=[], writes=["done"])
async def slow_step(state: State) -> State:
    """An action that takes longer than the test's timeout."""
    await asyncio.sleep(5.0)
    return state.update(done=True)


@action(reads=[], writes=["done"])
async def fast_step(state: State) -> State:
    """An action that finishes well under the test's timeout."""
    await asyncio.sleep(0.01)
    return state.update(done=True)


def _slow_app():
    return (
        ApplicationBuilder()
        .with_actions(slow_step=slow_step)
        .with_state(done=False)
        .with_entrypoint("slow_step")
        .build()
    )


def _fast_app():
    return (
        ApplicationBuilder()
        .with_actions(fast_step=fast_step)
        .with_state(done=False)
        .with_entrypoint("fast_step")
        .build()
    )


@pytest.mark.asyncio
async def test_slow_action_is_cancelled_and_returns_timeout_error():
    server = mount(
        _slow_app,
        mode=ServingMode.STEP,
        name="slow",
        action_timeout_seconds=0.2,
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "slow_step", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_timeout"
        assert out["requested"] == "slow_step"
        assert out["timeout_seconds"] == 0.2
        assert out["valid_next_actions"] == ["slow_step"]  # didn't advance


@pytest.mark.asyncio
async def test_timeout_is_recorded_in_history():
    server = mount(
        _slow_app,
        mode=ServingMode.STEP,
        name="slow-history",
        action_timeout_seconds=0.2,
    )
    async with Client(server) as client:
        await client.call_tool("step", {"action": "slow_step", "inputs": {}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1
        entry = history[0]
        assert entry["refused"] is True
        assert entry["refusal_reason"] == "action_timeout"
        assert entry["error_type"] == "TimeoutError"
        assert "0.2s timeout" in entry["error_message"]
        assert entry["state_after"] is None


@pytest.mark.asyncio
async def test_timeout_does_not_advance_state():
    server = mount(
        _slow_app,
        mode=ServingMode.STEP,
        name="slow-no-advance",
        action_timeout_seconds=0.2,
    )
    async with Client(server) as client:
        await client.call_tool("step", {"action": "slow_step", "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        # done should still be False, FSM still at entry.
        assert state.get("done") is False
        next_actions = json.loads((await client.read_resource("burr://next"))[0].text)
        assert next_actions == ["slow_step"]


@pytest.mark.asyncio
async def test_fast_action_under_timeout_succeeds_normally():
    server = mount(
        _fast_app,
        mode=ServingMode.STEP,
        name="fast",
        action_timeout_seconds=1.0,
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "fast_step", "inputs": {}})
        out = json.loads(r.content[0].text)
        # No error key; state advanced.
        assert "error" not in out
        assert out["state"]["done"] is True


@pytest.mark.asyncio
async def test_no_timeout_means_no_wait_for():
    """When action_timeout_seconds is None, slow actions complete fully."""

    @action(reads=[], writes=["done"])
    async def medium_step(state: State) -> State:
        await asyncio.sleep(0.15)
        return state.update(done=True)

    def medium_app():
        return (
            ApplicationBuilder()
            .with_actions(medium_step=medium_step)
            .with_state(done=False)
            .with_entrypoint("medium_step")
            .build()
        )

    server = mount(
        medium_app,
        mode=ServingMode.STEP,
        name="no-timeout",
        # action_timeout_seconds=None is the default.
    )
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "medium_step", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert "error" not in out
        assert out["state"]["done"] is True


@pytest.mark.asyncio
async def test_timeout_applies_in_tools_mode():
    """Per-action tools in TOOLS mode also honor the server-wide timeout."""
    server = mount(
        _slow_app,
        mode=ServingMode.TOOLS,
        name="slow-tools",
        action_timeout_seconds=0.2,
    )
    async with Client(server) as client:
        r = await client.call_tool("slow_step", {})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_timeout"
        assert out["timeout_seconds"] == 0.2
