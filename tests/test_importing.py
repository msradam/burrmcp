"""Importer: lift FastMCP tools into a Burr Application.

The lifted Application should behave like a hand-written one: same
transition enforcement, same history recording, same state semantics.
Tests cover sync + async tools, ``merge_result`` mode, explicit
``state_update`` callables, ``rename``, ``only`` filtering, and
unknown-entrypoint validation.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from burrmcp import ServingMode, ToolSpec, burr_app_from_fastmcp, mount


def _make_flat_server() -> FastMCP:
    flat = FastMCP("flat-test")

    @flat.tool
    async def create_order(item: str, qty: int = 1) -> dict:
        """Place an order."""
        return {"order_id": f"ORD-{item.upper()}", "item": item, "qty": qty}

    @flat.tool
    async def pay(order_id: str, amount: float) -> dict:
        """Pay for the order."""
        return {"paid": True, "paid_amount": amount}

    @flat.tool
    async def fulfill(order_id: str) -> dict:
        """Mark the order fulfilled."""
        return {"status": "fulfilled"}

    @flat.tool
    def health() -> str:
        """Out-of-band tool, not part of the FSM."""
        return "ok"

    return flat


@pytest.mark.asyncio
async def test_lifted_graph_enforces_transitions():
    flat = _make_flat_server()
    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="create_order",
        initial_state={"order_id": None, "paid": False},
        tool_specs={
            "create_order": ToolSpec(writes=["order_id", "item", "qty"], merge_result=True),
            "pay": ToolSpec(reads=["order_id"], writes=["paid", "paid_amount"], merge_result=True),
            "fulfill": ToolSpec(reads=["order_id", "paid"], writes=["status"], merge_result=True),
        },
        transitions=[("create_order", "pay"), ("pay", "fulfill")],
        only=["create_order", "pay", "fulfill"],
    )
    server = mount(app, mode=ServingMode.STEP, name="lifted")

    async with Client(server) as client:
        # Refusal: pay before create_order.
        r = await client.call_tool(
            "step", {"action": "pay", "inputs": {"order_id": "X", "amount": 5}}
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["create_order"]

        # Happy path.
        r = await client.call_tool(
            "step", {"action": "create_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["order_id"] == "ORD-LATTE"
        assert out["state"]["item"] == "latte"
        assert out["state"]["qty"] == 2
        assert out["valid_next_actions"] == ["pay"]

        r = await client.call_tool(
            "step", {"action": "pay", "inputs": {"order_id": "ORD-LATTE", "amount": 5.0}}
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["paid"] is True
        assert out["state"]["paid_amount"] == 5.0
        assert out["valid_next_actions"] == ["fulfill"]

        r = await client.call_tool(
            "step", {"action": "fulfill", "inputs": {"order_id": "ORD-LATTE"}}
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "fulfilled"
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_lifted_graph_records_history():
    flat = _make_flat_server()
    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="create_order",
        tool_specs={
            "create_order": ToolSpec(writes=["order_id"], merge_result=True),
        },
        only=["create_order"],
    )
    server = mount(app, mode=ServingMode.STEP, name="lifted-history")

    async with Client(server) as client:
        await client.call_tool("step", {"action": "create_order", "inputs": {"item": "mocha"}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1
        assert history[0]["action"] == "create_order"
        assert history[0]["state_after"]["order_id"] == "ORD-MOCHA"


@pytest.mark.asyncio
async def test_only_filter_excludes_listed_tools():
    flat = _make_flat_server()
    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="create_order",
        only=["create_order", "pay"],
        transitions=[("create_order", "pay")],
    )
    action_names = {a.name for a in app.graph.actions}
    assert action_names == {"create_order", "pay"}
    assert "health" not in action_names
    assert "fulfill" not in action_names


@pytest.mark.asyncio
async def test_rename_changes_action_name_in_graph():
    flat = _make_flat_server()
    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="place_order",
        only=["create_order"],
        tool_specs={
            "create_order": ToolSpec(rename="place_order", writes=["order_id"], merge_result=True),
        },
    )
    assert {a.name for a in app.graph.actions} == {"place_order"}


@pytest.mark.asyncio
async def test_unknown_entrypoint_raises_clear_error():
    flat = _make_flat_server()
    with pytest.raises(ValueError, match=r"entrypoint 'nonexistent' is not among"):
        await burr_app_from_fastmcp(
            flat,
            entrypoint="nonexistent",
            only=["create_order"],
        )


@pytest.mark.asyncio
async def test_state_update_callable_overrides_merge_result():
    """The explicit state_update callable wins over merge_result."""
    flat = FastMCP("custom-update")

    @flat.tool
    async def stamp() -> dict:
        return {"raw_thing": 1, "other": 2}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="stamp",
        initial_state={"stamped": False},
        tool_specs={
            "stamp": ToolSpec(
                writes=["stamped"],
                state_update=lambda result: {"stamped": True, "from_result": result["raw_thing"]},
            ),
        },
    )
    server = mount(app, mode=ServingMode.STEP, name="custom")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "stamp", "inputs": {}})
        out = json.loads(r.content[0].text)
        # state_update result wins; merge_result wasn't even set.
        assert out["state"]["stamped"] is True
        assert out["state"]["from_result"] == 1


@pytest.mark.asyncio
async def test_sync_tools_lift_correctly():
    """A FastMCP server with synchronous tools lifts the same way."""
    flat = FastMCP("sync-test")

    @flat.tool
    def go(x: int) -> dict:
        return {"y": x * 2}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="go",
        initial_state={"y": 0},
        tool_specs={"go": ToolSpec(writes=["y"], merge_result=True)},
    )
    server = mount(app, mode=ServingMode.STEP, name="sync")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "go", "inputs": {"x": 5}})
        out = json.loads(r.content[0].text)
        assert out["state"]["y"] == 10


@pytest.mark.asyncio
async def test_string_conditions_lifted_to_expressions():
    """A string condition in transitions is wrapped in Condition.expr."""
    flat = FastMCP("branch-test")

    @flat.tool
    async def classify(severity: str) -> dict:
        return {"severity": severity}

    @flat.tool
    async def escalate() -> dict:
        return {"escalated": True}

    @flat.tool
    async def queue() -> dict:
        return {"queued": True}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="classify",
        initial_state={"severity": None},
        tool_specs={
            "classify": ToolSpec(writes=["severity"], merge_result=True),
            "escalate": ToolSpec(reads=["severity"], writes=["escalated"], merge_result=True),
            "queue": ToolSpec(reads=["severity"], writes=["queued"], merge_result=True),
        },
        transitions=[
            ("classify", "escalate", "severity == 'urgent'"),
            ("classify", "queue", "severity == 'routine'"),
        ],
    )
    server = mount(app, mode=ServingMode.STEP, name="branch")

    async with Client(server) as client:
        await client.call_tool("step", {"action": "classify", "inputs": {"severity": "urgent"}})
        valid = json.loads((await client.read_resource("burr://next"))[0].text)
        assert valid == ["escalate"]


@pytest.mark.asyncio
async def test_tool_with_no_spec_lifts_as_stateless_action():
    """Tool not in tool_specs is wrapped with no reads/writes."""
    flat = FastMCP("stateless-test")

    @flat.tool
    async def noop() -> str:
        return "done"

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="noop",
        initial_state={"x": 1},
        # No tool_specs; noop has no state contract.
    )
    server = mount(app, mode=ServingMode.STEP, name="noop")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "noop", "inputs": {}})
        out = json.loads(r.content[0].text)
        # State unchanged because no writes declared.
        assert out["state"]["x"] == 1
        assert out["action"] == "noop"


@pytest.mark.asyncio
async def test_signature_preserved_through_lift():
    """Tool's parameter signature carries over to the lifted Burr action.

    A frontier-model client introspecting an action needs to see the
    actual parameter names + types from the original tool, not a
    flattened ``**kwargs``. STEP mode's step tool exposes a generic
    ``{action, inputs}`` shape on the MCP wire, but the Burr Action's
    declared inputs are what burrmcp uses to validate calls, so we
    introspect those directly.
    """
    flat = FastMCP("sig-test")

    @flat.tool
    async def make_order(item: str, qty: int = 1, note: str | None = None) -> dict:
        return {"item": item, "qty": qty, "note": note}

    app = await burr_app_from_fastmcp(
        flat,
        entrypoint="make_order",
        tool_specs={"make_order": ToolSpec(writes=["item", "qty"], merge_result=True)},
    )
    action = app.graph.get_action("make_order")
    required, optional = action.optional_and_required_inputs
    all_inputs = set(required) | set(optional)
    assert {"item", "qty", "note"}.issubset(all_inputs)
    assert "item" in required
    assert "qty" in optional
    assert "note" in optional
