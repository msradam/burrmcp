"""Tests for the coffee_order demo's non-linear branches.

The linear take_order -> pay -> fulfill path is well-covered by
test_step_mode.py and others. This file focuses on the add_modifier
loop and the cancel escape so a future change that breaks either
fails loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from coffee_order import build_application, build_server


@pytest.mark.asyncio
async def test_modifier_loop_accumulates_total():
    """Walk take_order -> add_modifier x3 -> pay -> fulfill and confirm
    the total reflects the modifiers."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )
        for modifier in ("extra_shot", "oat_milk", "syrup"):
            r = await client.call_tool(
                "step",
                {"action": "add_modifier", "inputs": {"modifier": modifier}},
            )
            out = r.structured_content
            assert out["action"] == "add_modifier"
            assert modifier in out["state"]["modifiers"]
        # Base $5 + 3x $1 = $8.
        assert out["state"]["total"] == pytest.approx(8.0)

        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 8.0}})
        out = r.structured_content
        assert out["state"]["stage"] == "paid"
        r = await client.call_tool("step", {"action": "fulfill", "inputs": {}})
        out = r.structured_content
        assert out["state"]["stage"] == "fulfilled"


@pytest.mark.asyncio
async def test_cancel_from_ordered_state():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )
        r = await client.call_tool("step", {"action": "cancel", "inputs": {}})
        out = r.structured_content
        assert out["state"]["stage"] == "cancelled"
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_cancel_from_modifier_state():
    """cancel is reachable mid-modifier-loop too, not just from take_order."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )
        await client.call_tool(
            "step", {"action": "add_modifier", "inputs": {"modifier": "extra_shot"}}
        )
        r = await client.call_tool("step", {"action": "cancel", "inputs": {}})
        out = r.structured_content
        assert out["state"]["stage"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unreachable_post_pay():
    """Once paid, the FSM only goes to fulfill. cancel is refused."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        r = await client.call_tool("step", {"action": "cancel", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["fulfill"]


def test_take_order_rejects_zero_qty():
    """The action-body refuses qty < 1 (raises ValueError, surfaces
    as action_error through the adapter)."""
    app = build_application()
    target = app.graph.get_action("take_order")
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        with pytest.raises(ValueError, match="qty must be >= 1"):
            app.step(inputs={"item": "latte", "qty": 0})
    finally:
        app.get_next_action = original


def test_initial_state_is_sparse():
    """Only `stage` lives in initial state; item/qty/modifiers/total
    appear once take_order writes them."""
    app = build_application()
    state = app.state.get_all()
    assert state.get("stage") == "new"
    # No drink-shaped keys before take_order fires.
    assert "item" not in state
    assert "modifiers" not in state
    assert "total" not in state
