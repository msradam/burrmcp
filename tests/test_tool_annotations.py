"""The four MCP tools carry the right annotations for capable clients."""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from theodosia import mount


@action(reads=[], writes=["x"])
def _go(state: State) -> State:
    return state.update(x=1)


def _factory():
    return ApplicationBuilder().with_actions(go=_go).with_state(x=0).with_entrypoint("go").build()


@pytest.mark.asyncio
async def test_step_is_destructive_and_not_idempotent():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        step = tools["step"]
        assert step.annotations is not None
        assert step.annotations.destructiveHint is True
        assert step.annotations.idempotentHint is False


@pytest.mark.asyncio
async def test_reset_session_is_destructive_and_idempotent():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        rs = tools["reset_session"]
        assert rs.annotations.destructiveHint is True
        assert rs.annotations.idempotentHint is True


@pytest.mark.asyncio
async def test_fork_at_is_destructive_open_world_false():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        fa = tools["fork_at"]
        assert fa.annotations.destructiveHint is True
        assert fa.annotations.openWorldHint is False


@pytest.mark.asyncio
async def test_read_resource_is_marked_read_only():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        rr = tools.get("read_resource")
        assert rr is not None, "ResourcesAsTools should add read_resource"
        # FastMCP's ResourcesAsTools transform marks the synthetic tools
        # as read-only; downstream clients can rely on that hint.
        assert rr.annotations.readOnlyHint is True
