"""Regression: ``action_timeout_seconds`` must preempt sync action bodies.

A sync action body that blocks (e.g. ``time.sleep``) would otherwise block
the event loop and defeat ``asyncio.wait_for``; the cancellation timer
cannot tick while the loop is blocked. Theodosia detects sync bodies and
runs them in a worker thread so the timeout fires regardless.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from theodosia import mount


@action(reads=[], writes=["x"])
def _sync_blocker(state: State) -> State:
    time.sleep(5.0)
    return state.update(x=1)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(slow=_sync_blocker)
        .with_state(x=0)
        .with_entrypoint("slow")
        .build()
    )


@pytest.mark.asyncio
async def test_sync_action_timeout_fires_within_budget():
    server = mount(_factory, name="t", action_timeout_seconds=0.5)
    start = asyncio.get_event_loop().time()
    async with Client(server) as c:
        r = await c.call_tool("step", {"action": "slow", "inputs": {}})
    elapsed = asyncio.get_event_loop().time() - start
    out = r.structured_content or {}
    assert out.get("error") == "action_timeout"
    # 0.5s budget plus epsilon for thread spin-up + client roundtrip.
    assert elapsed < 2.0, f"timeout did not preempt sync body; elapsed={elapsed:.2f}s"
