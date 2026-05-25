"""Tests for examples/async_hooks.py.

Validates the async hook variants (PreRunStepHookAsync,
PostRunStepHookAsync), the once-per-Application hook
(PostApplicationCreateHook), and the lifecycle-envelope hooks
(PreRunExecuteCallHookAsync, PostRunExecuteCallHookAsync).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from async_hooks import (
    AppCreatedHook,
    AsyncExecuteCallHook,
    AsyncTimingHook,
    build_application,
    build_server,
)


@pytest.mark.asyncio
async def test_async_timing_hook_fires_per_action():
    timing = AsyncTimingHook()
    app = build_application(timing_hook=timing)
    await app.arun(halt_after=["pick_best"])
    snap = timing.snapshot()
    assert set(snap.keys()) == {"fetch", "score", "pick_best"}
    for info in snap.values():
        assert info["runs"] == 1


def test_post_application_create_fires_once_per_build():
    hook = AppCreatedHook()
    assert hook.applications_created == 0
    build_application(app_create_hook=hook)
    assert hook.applications_created == 1
    build_application(app_create_hook=hook)
    assert hook.applications_created == 2


@pytest.mark.asyncio
async def test_execute_call_hooks_fire_on_arun():
    """arun walks the entire FSM in one call but Burr still fires
    pre/post execute_call once per underlying execute method
    (arun itself plus the per-step astep calls Burr makes internally)."""
    hook = AsyncExecuteCallHook()
    app = build_application(execute_call_hook=hook)
    await app.arun(halt_after=["pick_best"])
    assert hook.executes_started >= 1
    assert hook.executes_started == hook.executes_completed


@pytest.mark.asyncio
async def test_execute_call_hooks_fire_per_astep():
    """Each app.astep call is an execute boundary; both counters
    increment by one per step."""
    hook = AsyncExecuteCallHook()
    app = build_application(execute_call_hook=hook)
    for name in ("fetch", "score", "pick_best"):
        target = app.graph.get_action(name)
        original = app.get_next_action
        app.get_next_action = lambda t=target: t
        try:
            await app.astep()
        finally:
            app.get_next_action = original
    # Three steps -> three pre + three post fires.
    assert hook.executes_started == 3
    assert hook.executes_completed == 3


@pytest.mark.asyncio
async def test_full_async_walk_through_mcp_step():
    """Drive the async pipeline through MCP step; the MCP path uses
    astep internally so each step increments timing and execute_call
    counters by one."""
    server = build_server()
    async with Client(server) as client:
        for name, inputs in [
            ("fetch", {"count": 4, "latency_ms": 5}),
            ("score", {"latency_ms": 5}),
            ("pick_best", {}),
        ]:
            r = await client.call_tool("step", {"action": name, "inputs": inputs})
            out = r.structured_content
            assert out.get("error") is None, f"{name}: {out}"
        # Read the hooks resource.
        text = (await client.read_resource("theodosia://hooks"))[0].text
        hooks = json.loads(text)
        # Each step fires timing.
        assert "fetch" in hooks["timing_by_action"]
        assert "score" in hooks["timing_by_action"]
        # execute_call counters increment per MCP step (Theodosia's
        # adapter calls app.astep; each call is an execute boundary).
        assert hooks["executes_started"] >= 3
        assert hooks["executes_started"] == hooks["executes_completed"]
