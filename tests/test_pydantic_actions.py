"""Tests for examples/pydantic_actions.py.

Validates Burr's ``@pydantic_action`` decorator through mount(): the
subset-model machinery, Pydantic field validation on action inputs,
and the action-body refusal in finalize when address validation
fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from pydantic_actions import Address, Order, build_application, build_server  # noqa: E402


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_pydantic_models_carry_constraints():
    """Sanity: Field constraints fire on the underlying models."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Address(line1="", city="Oakland", region="CA", postal_code="94601")
    with pytest.raises(ValidationError):
        Order(qty=-1)


def test_full_walk_lands_finalized():
    app = build_application()
    _force_step(
        app,
        "place",
        customer_email="a@example.com",
        item_sku="SKU-1",
        qty=2,
        unit_price=10.0,
        address_line1="1 Main",
        address_city="Oakland",
        address_region="CA",
        address_postal="94601",
    )
    assert app.state["stage"] == "placed"
    _force_step(app, "validate_address")
    assert app.state["address_validated"] is True
    _force_step(app, "compute_shipping")
    # qty=2 with CA region: base 5 + 1*2 + 0 surcharge = 7.0
    assert app.state["shipping_cost"] == pytest.approx(7.0)
    _force_step(app, "compute_total")
    # subtotal = 2*10 = 20; total = 20 + 7 = 27
    assert app.state["subtotal"] == pytest.approx(20.0)
    assert app.state["total"] == pytest.approx(27.0)
    _force_step(app, "finalize")
    assert app.state["stage"] == "finalized"


def test_out_of_region_adds_surcharge():
    """Non-CA region triggers the $10 shipping surcharge in
    compute_shipping. The subset model carrying shipping_address
    flows through correctly."""
    app = build_application()
    _force_step(
        app,
        "place",
        customer_email="b@example.com",
        item_sku="SKU-2",
        qty=1,
        unit_price=20.0,
        address_line1="5 Other St",
        address_city="Portland",
        address_region="OR",
        address_postal="97201",
    )
    _force_step(app, "validate_address")
    _force_step(app, "compute_shipping")
    # base 5 + 1*1 + 10 surcharge = 16
    assert app.state["shipping_cost"] == pytest.approx(16.0)


def test_finalize_refuses_on_failed_validation():
    """If address validation fails (notes non-empty), finalize raises."""
    app = build_application()
    _force_step(
        app,
        "place",
        customer_email="c@example.com",
        item_sku="SKU-3",
        qty=1,
        unit_price=5.0,
        address_line1="9 St",
        address_city="Oakland",
        address_region="ca",  # lowercase -> validate_address records failure
        address_postal="94601",
    )
    _force_step(app, "validate_address")
    assert app.state["address_validated"] is False
    _force_step(app, "compute_shipping")
    _force_step(app, "compute_total")
    with pytest.raises(ValueError, match="address validation failed"):
        _force_step(app, "finalize")


@pytest.mark.asyncio
async def test_graph_resource_carries_pydantic_schema_for_order():
    server = build_server()
    async with Client(server) as client:
        text = (await client.read_resource("burr://graph"))[0].text
        graph = json.loads(text)
        schema = graph["state_schema"]
        assert schema is not None
        props = schema["properties"]
        # The Order model's fields appear with their types.
        assert "customer_email" in props
        assert "qty" in props
        assert "shipping_address" in props


@pytest.mark.asyncio
async def test_mcp_walk_to_finalized():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "place",
                "inputs": {
                    "customer_email": "d@example.com",
                    "item_sku": "SKU-4",
                    "qty": 1,
                    "unit_price": 50.0,
                    "address_line1": "11 Front",
                    "address_city": "Sacramento",
                    "address_region": "CA",
                    "address_postal": "95814",
                },
            },
        )
        for name in ("validate_address", "compute_shipping", "compute_total", "finalize"):
            r = await client.call_tool("step", {"action": name, "inputs": {}})
            out = json.loads(r.content[0].text)
            assert out.get("error") is None, f"{name}: {out}"
        assert out["state"]["stage"] == "finalized"
