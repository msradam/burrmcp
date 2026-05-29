"""``mount(hooks=[...])``: pass-through to Burr's lifecycle adapter set."""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.lifecycle import PostRunStepHook, PreRunStepHook
from fastmcp import Client

from theodosia import mount


@action(reads=[], writes=["x"])
def _go(state: State) -> State:
    return state.update(x=1)


def _factory():
    return ApplicationBuilder().with_actions(go=_go).with_state(x=0).with_entrypoint("go").build()


class _SpyHook(PostRunStepHook):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post_run_step(self, *, action, **kwargs):
        self.calls.append(action.name)


class _PreSpyHook(PreRunStepHook):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pre_run_step(self, *, action, **kwargs):
        self.calls.append(action.name)


@pytest.mark.asyncio
async def test_post_run_step_hook_fires_via_mount_kwarg():
    hook = _SpyHook()
    server = mount(_factory, name="t", hooks=[hook])
    async with Client(server) as c:
        await c.call_tool("step", {"action": "go", "inputs": {}})
    assert hook.calls == ["go"]


@pytest.mark.asyncio
async def test_pre_run_step_hook_also_fires():
    hook = _PreSpyHook()
    server = mount(_factory, name="t", hooks=[hook])
    async with Client(server) as c:
        await c.call_tool("step", {"action": "go", "inputs": {}})
    assert hook.calls == ["go"]


@pytest.mark.asyncio
async def test_multiple_hooks_compose():
    pre = _PreSpyHook()
    post = _SpyHook()
    server = mount(_factory, name="t", hooks=[pre, post])
    async with Client(server) as c:
        await c.call_tool("step", {"action": "go", "inputs": {}})
        await c.call_tool("step", {"action": "go", "inputs": {}})
    # Each session's app is rebuilt, but pre/post fire on every step within.
    assert len(pre.calls) >= 1
    assert len(post.calls) >= 1


@pytest.mark.asyncio
async def test_hooks_none_is_identity():
    """Passing hooks=None or omitting it must not change behavior."""
    server = mount(_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool("step", {"action": "go", "inputs": {}})
    assert r.structured_content.get("state", {}).get("x") == 1
