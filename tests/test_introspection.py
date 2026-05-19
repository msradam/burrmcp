"""Pure-Python tests on the adapter's introspection helpers.

No FastMCP client; no server. Just verifies that the helpers we use to
build tool schemas and decide valid transitions behave on the
coffee-order FSM.
"""

from __future__ import annotations

from burr_mcp.adapter import (
    _action_inputs,
    _action_signature_params,
    _public_state,
    valid_next_action_names,
)


def test_valid_next_actions_at_entrypoint(fresh_app):
    # Fresh app: PRIOR_STEP not set → valid next is the entrypoint.
    assert valid_next_action_names(fresh_app) == ["take_order"]


def test_valid_next_actions_after_take_order(fresh_app):
    import asyncio

    asyncio.run(fresh_app.astep(inputs={"item": "latte", "qty": 1}))
    assert valid_next_action_names(fresh_app) == ["pay"]


def test_valid_next_actions_terminal(fresh_app):
    import asyncio

    asyncio.run(fresh_app.astep(inputs={"item": "latte", "qty": 1}))
    asyncio.run(fresh_app.astep(inputs={"amount": 4.5}))
    asyncio.run(fresh_app.astep(inputs={}))
    # fulfill has no outgoing transitions
    assert valid_next_action_names(fresh_app) == []


def test_action_inputs_required_and_optional(fresh_app):
    take = fresh_app.graph.get_action("take_order")
    req, opt = _action_inputs(take)
    assert req == ["item"]
    assert opt == ["qty"]


def test_action_signature_params_carries_types_and_defaults(fresh_app):
    take = fresh_app.graph.get_action("take_order")
    params = _action_signature_params(take)
    by_name = {p.name: p for p in params}
    assert by_name["item"].annotation is str
    assert by_name["qty"].annotation is int
    assert by_name["qty"].default == 1


def test_public_state_filters_internal_keys():
    raw = {
        "stage": "ordered",
        "item": "latte",
        "__PRIOR_STEP": "take_order",
        "__SEQUENCE_ID": 0,
    }
    cleaned = _public_state(raw)
    assert cleaned == {"stage": "ordered", "item": "latte"}
