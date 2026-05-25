"""external_tools: the Burr graph as a cross-MCP-server playbook.

A mount(external_tools={action: [tool, ...]}) declares which tools on
OTHER connected MCP servers are relevant when each action is a reachable
next move. Theodosia surfaces them per-action in theodosia://graph and
contextually as next_external_tools in each step response. Theodosia does
not execute these tools; it sequences them.
"""

from __future__ import annotations

import json

import pytest
from coffee_order import build_application
from fastmcp import Client

from theodosia import ServingMode, mount
from theodosia.adapter import (
    _next_external_tools,
    _normalize_external_tools,
)

# coffee_order actions: take_order, add_modifier, pay, fulfill, cancel.
_EXT = {
    "take_order": ["menu_lookup", "inventory_check"],
    "pay": ["payment_gateway_charge"],
    "fulfill": ["kitchen_ticket_print"],
}


def _server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="coffee-ext",
        external_tools=_EXT,
    )


async def _step(client, action, **inputs):
    return (await client.call_tool("step", {"action": action, "inputs": inputs})).structured_content


# == normalization (unit) =============================================


def test_normalize_keeps_known_actions():
    app = build_application()
    out = _normalize_external_tools(_EXT, app)
    assert set(out) == {"take_order", "pay", "fulfill"}
    assert out["take_order"] == ["menu_lookup", "inventory_check"]


def test_normalize_drops_unknown_actions_with_warning():
    app = build_application()
    with pytest.warns(UserWarning, match="unknown action"):
        out = _normalize_external_tools({"nonexistent": ["x"]}, app)
    assert out == {}


def test_normalize_strips_empty_tool_names():
    app = build_application()
    out = _normalize_external_tools({"pay": ["a", "", "  ", "b"]}, app)
    assert out["pay"] == ["a", "b"]


def test_next_external_tools_filters_to_reachable():
    m = {"take_order": ["a"], "pay": ["b"], "fulfill": ["c"]}
    # Only pay + fulfill reachable now.
    out = _next_external_tools(m, ["pay", "fulfill"])
    assert out == {"pay": ["b"], "fulfill": ["c"]}
    # take_order not reachable -> omitted.
    assert "take_order" not in out


def test_next_external_tools_empty_when_no_declared_tools():
    m = {"pay": ["b"]}
    # cancel has no declared external tools -> empty dict (caller omits).
    assert _next_external_tools(m, ["cancel"]) == {}


# == theodosia://graph surfacing ===========================================


@pytest.mark.asyncio
async def test_graph_resource_carries_per_action_external_tools():
    async with Client(_server()) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        by_name = {a["name"]: a for a in graph["actions"]}
        assert by_name["take_order"]["external_tools"] == ["menu_lookup", "inventory_check"]
        assert by_name["pay"]["external_tools"] == ["payment_gateway_charge"]
        # An action with no declared external tools omits the key entirely.
        assert "external_tools" not in by_name["cancel"]


@pytest.mark.asyncio
async def test_graph_resource_omits_external_tools_when_not_configured():
    server = mount(build_application, mode=ServingMode.STEP, name="coffee-plain")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        for a in graph["actions"]:
            assert "external_tools" not in a


# == step response surfacing ==========================================


@pytest.mark.asyncio
async def test_step_success_surfaces_next_external_tools():
    """After take_order, pay is reachable -> its external tools appear."""
    async with Client(_server()) as client:
        out = await _step(client, "take_order", item="latte", qty=1)
        assert "error" not in out
        net = out.get("next_external_tools")
        assert net is not None
        # pay is reachable from 'ordered'; its declared tool shows up.
        assert net.get("pay") == ["payment_gateway_charge"]


@pytest.mark.asyncio
async def test_step_invalid_transition_still_surfaces_next_external_tools():
    """Refusals carry next_external_tools for the reachable actions too."""
    async with Client(_server()) as client:
        # pay before ordering -> invalid_transition; valid is take_order.
        out = await _step(client, "pay", amount=5)
        assert out["error"] == "invalid_transition"
        net = out.get("next_external_tools")
        assert net is not None
        assert net.get("take_order") == ["menu_lookup", "inventory_check"]


@pytest.mark.asyncio
async def test_step_omits_next_external_tools_when_not_configured():
    server = mount(build_application, mode=ServingMode.STEP, name="coffee-plain2")
    async with Client(server) as client:
        out = await _step(client, "take_order", item="latte", qty=1)
        assert "next_external_tools" not in out
