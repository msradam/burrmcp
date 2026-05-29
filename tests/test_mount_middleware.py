"""``mount(middleware=[...])``: pass-through to FastMCP's middleware chain."""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client
from fastmcp.server.middleware import Middleware, MiddlewareContext

from theodosia import mount


@action(reads=[], writes=["x"])
def _go(state: State) -> State:
    return state.update(x=1)


def _factory():
    return ApplicationBuilder().with_actions(go=_go).with_state(x=0).with_entrypoint("go").build()


class _SpyMW(Middleware):
    def __init__(self) -> None:
        self.tool_calls: list[str] = []

    async def on_call_tool(self, ctx: MiddlewareContext, call_next):
        self.tool_calls.append(ctx.message.name)
        return await call_next(ctx)


@pytest.mark.asyncio
async def test_user_middleware_sees_step_calls():
    spy = _SpyMW()
    server = mount(_factory, name="t", middleware=[spy])
    async with Client(server) as c:
        await c.call_tool("step", {"action": "go", "inputs": {}})
        await c.call_tool("step", {"action": "go", "inputs": {}})
    assert spy.tool_calls == ["step", "step"]


@pytest.mark.asyncio
async def test_middleware_none_is_identity():
    """Omitting middleware must not break the default chain."""
    server = mount(_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool("step", {"action": "go", "inputs": {}})
    assert r.structured_content.get("state", {}).get("x") == 1


@pytest.mark.asyncio
async def test_multiple_middleware_all_fire():
    a, b = _SpyMW(), _SpyMW()
    server = mount(_factory, name="t", middleware=[a, b])
    async with Client(server) as c:
        await c.call_tool("step", {"action": "go", "inputs": {}})
    assert a.tool_calls == ["step"]
    assert b.tool_calls == ["step"]
