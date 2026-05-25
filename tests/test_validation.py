"""Input validators: refuse calls before they touch the action.

Validators run after the transition check but before the action's
wrapped function. They get ``(state_dict, inputs)``, can return a
dict to substitute normalised inputs, return None to accept the
originals, or raise ``ValidationFailed`` to refuse the call.

The refusal is recorded as ``refusal_reason: "validation_failed"`` in
``theodosia://history`` with the reason and details preserved; the FSM
does not advance.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client, FastMCP

from theodosia import (
    ServingMode,
    ToolSpec,
    ValidationFailed,
    burr_app_from_fastmcp,
    mount,
)

# ── server-wide input_validators mapping ─────────────────────────────


@action(reads=[], writes=["item", "qty"])
async def place_order(state: State, item: str, qty: int) -> State:
    return state.update(item=item, qty=qty)


def _order_app():
    return (
        ApplicationBuilder()
        .with_actions(place_order=place_order)
        .with_state(item=None, qty=None)
        .with_entrypoint("place_order")
        .build()
    )


def _qty_validator(state_dict: dict, inputs: dict) -> dict | None:
    if inputs.get("qty", 0) <= 0:
        raise ValidationFailed(
            "qty must be positive",
            details={"field": "qty", "received": inputs.get("qty")},
        )
    return None


@pytest.mark.asyncio
async def test_validator_refusal_returns_structured_error():
    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="qty-server",
        input_validators={"place_order": _qty_validator},
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "latte", "qty": -1}},
        )
        out = r.structured_content
        assert out["error"] == "validation_failed"
        assert out["reason"] == "qty must be positive"
        assert out["details"]["field"] == "qty"
        assert out["details"]["received"] == -1


@pytest.mark.asyncio
async def test_validator_refusal_doesnt_advance_state():
    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="qty-no-advance",
        input_validators={"place_order": _qty_validator},
    )
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "latte", "qty": -1}},
        )
        state = json.loads((await client.read_resource("theodosia://state"))[0].text)
        assert state.get("item") is None
        next_actions = json.loads((await client.read_resource("theodosia://next"))[0].text)
        assert next_actions == ["place_order"]


@pytest.mark.asyncio
async def test_validator_refusal_recorded_in_history():
    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="qty-history",
        input_validators={"place_order": _qty_validator},
    )
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "latte", "qty": 0}},
        )
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        assert len(history) == 1
        entry = history[0]
        assert entry["refused"] is True
        assert entry["refusal_reason"] == "validation_failed"
        assert entry["error_type"] == "ValidationFailed"
        assert entry["error_message"] == "qty must be positive"


@pytest.mark.asyncio
async def test_validator_accepting_advances_normally():
    """When validation passes, the call proceeds and state advances."""
    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="qty-accept",
        input_validators={"place_order": _qty_validator},
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "latte", "qty": 1}},
        )
        out = r.structured_content
        assert "error" not in out
        assert out["state"]["qty"] == 1


@pytest.mark.asyncio
async def test_validator_can_substitute_normalised_inputs():
    """Returning a dict from the validator overrides the inputs the
    action's fn sees."""

    def normalize(state_dict, inputs):
        return {**inputs, "item": inputs["item"].lower()}

    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="normalize",
        input_validators={"place_order": normalize},
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "LATTE", "qty": 1}},
        )
        out = r.structured_content
        assert out["state"]["item"] == "latte"


@pytest.mark.asyncio
async def test_async_validators_supported():
    """Validators may be coroutine functions."""

    async def async_validator(state_dict, inputs):
        if inputs["qty"] > 100:
            raise ValidationFailed("qty too large")
        return

    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="async-validator",
        input_validators={"place_order": async_validator},
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "place_order", "inputs": {"item": "latte", "qty": 999}},
        )
        out = r.structured_content
        assert out["error"] == "validation_failed"
        assert out["reason"] == "qty too large"


# ── ToolSpec validator via the importer ──────────────────────────────


@pytest.mark.asyncio
async def test_tool_spec_validator_wires_through_importer():
    flat = FastMCP("validator-spec")

    @flat.tool
    async def buy(item: str, amount: float) -> dict:
        return {"item": item, "amount": amount}

    def positive_amount(state, inputs):
        if inputs["amount"] <= 0:
            raise ValidationFailed("amount must be > 0")
        return

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="buy",
        tool_specs={
            "buy": ToolSpec(
                writes=["item", "amount"],
                merge_result=True,
                validator=positive_amount,
            ),
        },
    )
    server = mount(app, mode=ServingMode.STEP, name="lifted-validator")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "buy", "inputs": {"item": "x", "amount": -5}})
        out = r.structured_content
        assert out["error"] == "validation_failed"
        assert out["reason"] == "amount must be > 0"


# ── hand-tagged validator on a Burr-decorated function ──────────────


@pytest.mark.asyncio
async def test_hand_tagged_validator_via_function_attribute():
    """Setting _theodosia_validator on a decorated function works the
    same as ToolSpec.validator or input_validators={}."""

    def positive_qty(state, inputs):
        if inputs["qty"] < 1:
            raise ValidationFailed("qty must be >= 1")
        return

    @action(reads=[], writes=["qty"])
    async def tagged(state: State, qty: int) -> State:
        return state.update(qty=qty)

    tagged._theodosia_validator = positive_qty  # type: ignore[attr-defined]

    def factory():
        return (
            ApplicationBuilder()
            .with_actions(tagged=tagged)
            .with_state(qty=None)
            .with_entrypoint("tagged")
            .build()
        )

    server = mount(factory, mode=ServingMode.STEP, name="hand-tagged-v")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "tagged", "inputs": {"qty": 0}})
        out = r.structured_content
        assert out["error"] == "validation_failed"


# ── validator returning a non-dict is itself flagged ────────────────


@pytest.mark.asyncio
async def test_validator_returning_non_dict_is_caught():
    """If a validator mis-returns (e.g. accidentally returns ``"ok"``),
    the adapter raises a clean ValidationFailed rather than feeding
    junk into the action's fn."""

    def bad_validator(state, inputs):
        return "this is not a dict"

    server = mount(
        _order_app,
        mode=ServingMode.STEP,
        name="bad-validator",
        input_validators={"place_order": bad_validator},
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "place_order", "inputs": {"item": "latte", "qty": 1}}
        )
        out = r.structured_content
        assert out["error"] == "validation_failed"
        assert "non-dict" in out["reason"]
