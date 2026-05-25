"""Typed state via Pydantic surfaces in theodosia://graph.

When the user wires up Burr's ``PydanticTypingSystem``, the typing
system carries the full Pydantic model. ``theodosia://graph`` exports the
model's JSON schema under ``state_schema`` so an MCP client gets
typed shape information without having to introspect each action's
writes.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.integrations.pydantic import PydanticTypingSystem
from fastmcp import Client
from pydantic import BaseModel

from theodosia import ServingMode, mount


class OrderState(BaseModel):
    item: str | None = None
    qty: int = 0
    paid: bool = False
    notes: list[str] = []


@action(reads=[], writes=["item", "qty"])
async def order(state: State, item: str, qty: int) -> State:
    return state.update(item=item, qty=qty)


@action(reads=["item", "qty"], writes=["paid"])
async def pay(state: State, amount: float) -> State:
    return state.update(paid=True)


def typed_factory():
    return (
        ApplicationBuilder()
        .with_typing(PydanticTypingSystem(OrderState))
        .with_actions(order=order, pay=pay)
        .with_transitions(("order", "pay"))
        .with_state(OrderState())
        .with_entrypoint("order")
        .build()
    )


def untyped_factory():
    return (
        ApplicationBuilder()
        .with_actions(order=order, pay=pay)
        .with_transitions(("order", "pay"))
        .with_state(item=None, qty=0, paid=False)
        .with_entrypoint("order")
        .build()
    )


@pytest.mark.asyncio
async def test_typed_state_schema_surfaces_in_graph():
    server = mount(typed_factory, mode=ServingMode.STEP, name="typed-test")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        schema = graph["state_schema"]
        assert schema is not None
        assert schema["title"] == "OrderState"
        props = schema["properties"]
        assert "item" in props
        assert "qty" in props
        assert "paid" in props
        assert "notes" in props
        # Pydantic should have produced typed entries.
        assert props["qty"]["type"] == "integer"
        assert props["paid"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_untyped_state_schema_is_null():
    server = mount(untyped_factory, mode=ServingMode.STEP, name="untyped-test")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        assert graph["state_schema"] is None


@pytest.mark.asyncio
async def test_typed_application_runs_normally():
    """A typed Application served via theodosia.mount works the same
    as an untyped one. State stays Pydantic-validated internally;
    the wire-level shape is the same JSON dict."""
    server = mount(typed_factory, mode=ServingMode.STEP, name="typed-run")
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "order", "inputs": {"item": "latte", "qty": 2}},
        )
        out = r.structured_content
        assert out["state"]["item"] == "latte"
        assert out["state"]["qty"] == 2
