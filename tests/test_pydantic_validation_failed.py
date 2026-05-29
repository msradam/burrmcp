"""Pydantic-typed inputs surface validation errors as ``validation_failed``.

When a typed input parameter cannot construct its declared model, the
client must see a clean ``validation_failed`` refusal with per-field
details, not an opaque ``action_error`` from the action body crashing
on ``dict.method_call``.
"""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client
from pydantic import BaseModel

from theodosia import mount


class _OrderInput(BaseModel):
    item: str
    qty: int


@action(reads=[], writes=["order"])
def _take_order(state: State, order: _OrderInput) -> State:
    return state.update(order=order.model_dump())


@action(reads=[], writes=["x"])
def _opt_taker(state: State, order: _OrderInput | None = None) -> State:
    return state.update(x=order.item if order else None)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(take_order=_take_order)
        .with_state(order=None)
        .with_entrypoint("take_order")
        .build()
    )


def _opt_factory():
    return (
        ApplicationBuilder()
        .with_actions(opt=_opt_taker)
        .with_state(x=None)
        .with_entrypoint("opt")
        .build()
    )


@pytest.mark.asyncio
async def test_wrong_type_field_surfaces_validation_failed():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool(
            "step",
            {"action": "take_order", "inputs": {"order": {"item": 12345, "qty": "two"}}},
        )
    out = r.structured_content or {}
    assert out.get("error") == "validation_failed"
    assert "order" in out.get("reason", "")
    details = out.get("details") or {}
    assert details.get("model") == "_OrderInput"
    assert len(details.get("errors") or []) >= 1


@pytest.mark.asyncio
async def test_optional_pydantic_parameter_coerces():
    server = mount(_opt_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool(
            "step",
            {"action": "opt", "inputs": {"order": {"item": "soda", "qty": 1}}},
        )
    out = r.structured_content or {}
    assert out.get("state", {}).get("x") == "soda"
