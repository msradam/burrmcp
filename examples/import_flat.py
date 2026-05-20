"""Lift a flat FastMCP server into a Burr-backed MCP server.

Starting point: an existing FastMCP server with three tools and no
notion of state or order. Tools can be called in any sequence; nothing
prevents ``pay`` before ``create_order``.

Result: the same three tools, now backed by a Burr Application that
enforces ``create_order -> pay -> fulfill``, records every attempt
(success and refusal) in ``burr://history``, and exposes the current
order state at ``burr://state``.

The user supplies two declarations:

  • ``initial_state``: starting values for the shared state keys.
  • ``tool_specs``: per-tool, which state keys it reads and writes,
    and whether its return dict should merge into state.
  • ``transitions``: the legal order, optionally with conditions.

Everything else (tool signatures, docstrings, async/sync) carries over.

Run:

    python examples/import_flat.py
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from burrmcp import ServingMode, ToolSpec, burr_app_from_fastmcp, mount

# ── starting point: a flat FastMCP server ────────────────────────────

flat = FastMCP("legacy-coffee")


@flat.tool
async def create_order(item: str, qty: int = 1) -> dict:
    """Place a new coffee order. Returns the order id and item."""
    return {"order_id": f"ORD-{item.upper()}-{qty}", "item": item, "qty": qty}


@flat.tool
async def pay(order_id: str, amount: float) -> dict:
    """Pay for a placed order."""
    return {"paid": True, "paid_amount": amount, "receipt": f"R-{order_id}"}


@flat.tool
async def fulfill(order_id: str) -> dict:
    """Mark a paid order as fulfilled."""
    return {"status": "fulfilled", "fulfilled_order": order_id}


# ── lift into a Burr Application ─────────────────────────────────────


async def build_application():
    return await burr_app_from_fastmcp(
        flat,
        entrypoint="create_order",
        initial_state={
            "order_id": None,
            "item": None,
            "qty": None,
            "paid": False,
            "paid_amount": None,
            "status": None,
        },
        tool_specs={
            "create_order": ToolSpec(
                writes=["order_id", "item", "qty"],
                merge_result=True,
            ),
            "pay": ToolSpec(
                reads=["order_id"],
                writes=["paid", "paid_amount"],
                merge_result=True,
            ),
            "fulfill": ToolSpec(
                reads=["order_id", "paid"],
                writes=["status"],
                merge_result=True,
            ),
        },
        transitions=[
            ("create_order", "pay"),
            ("pay", "fulfill"),
        ],
    )


# ── re-serve via burr-mcp ────────────────────────────────────────────


def build_server():
    # Wrap in a zero-arg factory so each connecting session gets its
    # own Application instance (one customer's order isn't visible to
    # another customer). ``burr_app_from_fastmcp`` is async; we run a
    # fresh event loop per call to satisfy the factory's sync signature.
    return mount(
        lambda: asyncio.run(build_application()),
        mode=ServingMode.STEP,
        name="coffee-lifted",
        instructions=(
            "Coffee-order FSM lifted from a flat FastMCP server. "
            "Same three tools, now order-aware: create_order, pay, "
            "fulfill must be called in that order. Read burr://state "
            "for the current order and burr://next for valid next "
            "actions."
        ),
    )


if __name__ == "__main__":
    build_server().run()
