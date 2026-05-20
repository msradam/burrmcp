"""Tests for mount_multi: one MCP server hosting multiple Burr Applications.

FastMCP's native server-composition does the namespacing; mount_multi
is the thin glue that wraps each Burr Application via mount() and
attaches it to a parent FastMCP with namespace=<app_name>. Tools and
resources land at <app>_<tool> and burr://<app>/<path>.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from burrmcp import ServingMode, mount_multi


@action(reads=[], writes=["status", "value"])
def open_session(state: State, value: int = 0) -> State:
    return state.update(status="open", value=value)


@action(reads=["value"], writes=["value", "status"])
def close_session(state: State) -> State:
    return state.update(value=state["value"] + 1, status="closed")


def _factory_a():
    return (
        ApplicationBuilder()
        .with_actions(open_session=open_session, close_session=close_session)
        .with_transitions(("open_session", "close_session"))
        .with_state(status="initial", value=0)
        .with_entrypoint("open_session")
        .build()
    )


def _factory_b():
    return (
        ApplicationBuilder()
        .with_actions(open_session=open_session, close_session=close_session)
        .with_transitions(("open_session", "close_session"))
        .with_state(status="initial", value=100)
        .with_entrypoint("open_session")
        .build()
    )


# == argument validation ==========================================


def test_mount_multi_rejects_empty_dict():
    with pytest.raises(ValueError, match="at least one"):
        mount_multi({})


@pytest.mark.parametrize("bad_name", ["1starts_with_digit", "has-dash", "has space", "has.dot", ""])
def test_mount_multi_rejects_invalid_namespace_names(bad_name):
    with pytest.raises(ValueError, match="namespace"):
        mount_multi({bad_name: _factory_a})


# == namespacing surfacing ========================================


@pytest.mark.asyncio
async def test_apps_resource_lists_mounted_apps():
    server = mount_multi(
        {"order": _factory_a, "review": _factory_b},
        mode=ServingMode.STEP,
        name="multi-test",
    )
    async with Client(server) as client:
        r = await client.read_resource("burr://apps")
        out = json.loads(r[0].text)
        assert out["apps"] == ["order", "review"]


@pytest.mark.asyncio
async def test_namespaced_step_tool_exists_per_app():
    server = mount_multi(
        {"order": _factory_a, "review": _factory_b},
        mode=ServingMode.STEP,
    )
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "order_step" in names
        assert "review_step" in names
        assert "order_reset_session" in names
        assert "review_reset_session" in names


@pytest.mark.asyncio
async def test_namespaced_graph_resource_per_app():
    server = mount_multi({"order": _factory_a, "review": _factory_b})
    async with Client(server) as client:
        order_graph = json.loads((await client.read_resource("burr://order/graph"))[0].text)
        review_graph = json.loads((await client.read_resource("burr://review/graph"))[0].text)
        assert order_graph["name"] == "order"
        assert review_graph["name"] == "review"


# == per-app state isolation ======================================


@pytest.mark.asyncio
async def test_apps_have_independent_state():
    """Calling step on app A must not affect app B's state."""
    server = mount_multi({"order": _factory_a, "review": _factory_b})
    async with Client(server) as client:
        # Drive `order` forward.
        r = await client.call_tool(
            "order_step",
            {"action": "open_session", "inputs": {"value": 7}},
        )
        order_after = json.loads(r.content[0].text)
        assert order_after["state"]["value"] == 7

        # `review` should still be at its initial state.
        review_state = json.loads((await client.read_resource("burr://review/state"))[0].text)
        assert review_state["value"] == 100
        assert review_state["status"] == "initial"


@pytest.mark.asyncio
async def test_namespaced_step_returns_app_id_per_app():
    server = mount_multi({"order": _factory_a, "review": _factory_b})
    async with Client(server) as client:
        ra = await client.call_tool("order_step", {"action": "open_session", "inputs": {}})
        rb = await client.call_tool("review_step", {"action": "open_session", "inputs": {}})
        a_payload = json.loads(ra.content[0].text)
        b_payload = json.loads(rb.content[0].text)
        assert a_payload["app_id"] != b_payload["app_id"]


# == parent instructions ==========================================


def test_parent_instructions_mention_each_app():
    server = mount_multi(
        {"order": _factory_a, "review": _factory_b},
        instructions="Greeter line.",
    )
    instr = getattr(server, "instructions", None) or getattr(
        server._mcp_server, "instructions", None
    )
    assert instr is not None
    assert "Greeter line." in instr
    assert "order" in instr
    assert "review" in instr
    assert "burr://apps" in instr
