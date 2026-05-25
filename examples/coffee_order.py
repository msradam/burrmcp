"""Coffee-order FSM: a small but non-linear example.

Shape:

    take_order --> [add_modifier loop] --> pay --> fulfill
                                       \\-> cancel
                       \\-> cancel
       \\-> cancel

Features it exercises:

* A loop (``add_modifier`` repeats until the agent moves on).
* A ``cancel`` escape reachable from any pre-pay state.
* A ``Literal``-typed input (``modifier`` surfaces as a JSON Schema
  enum so the caller LLM sees the valid choices).
* A running ``total`` computed in state and visible to the agent.

Five actions, two terminal stages (``fulfilled`` and ``cancelled``).

Run as a server:

    python examples/coffee_order.py

Inspect from another shell with the FastMCP client or any MCP client.
``theodosia://state`` shows the order's current shape; ``theodosia://next``
lists the legal next actions.
"""

from __future__ import annotations

from typing import Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ServingMode, mount, tracker

_TRACKER_PROJECT = "coffee-order-demo"
_BASE_PRICE = 5.0
_MODIFIER_PRICE = {"extra_shot": 1.0, "oat_milk": 1.0, "syrup": 1.0}


@action(reads=[], writes=["stage", "item", "qty", "modifiers", "total"])
def take_order(state: State, item: str, qty: int = 1) -> State:
    """Place a new coffee order.

    Args:
        item: Drink name, e.g. ``"latte"``, ``"americano"``.
        qty: Number of drinks; defaults to 1.
    """
    if qty < 1:
        raise ValueError(f"qty must be >= 1; got {qty}")
    return state.update(
        stage="ordered",
        item=item,
        qty=qty,
        modifiers=[],
        total=_BASE_PRICE * qty,
    )


@action(reads=["modifiers", "total"], writes=["modifiers", "total"])
def add_modifier(
    state: State,
    modifier: Literal["extra_shot", "oat_milk", "syrup"],
) -> State:
    """Add one modifier to the order. Loops until the agent moves on.

    Args:
        modifier: ``"extra_shot"`` / ``"oat_milk"`` / ``"syrup"``.
            Each adds $1.00 to the running ``total``.
    """
    return state.update(
        modifiers=[*state["modifiers"], modifier],
        total=state["total"] + _MODIFIER_PRICE[modifier],
    )


@action(reads=["stage"], writes=["stage", "paid_amount"])
def pay(state: State, amount: float) -> State:
    """Pay for the placed order.

    Args:
        amount: Payment amount in whatever currency the cafe uses.
            The agent can read the expected total from ``state.total``.
    """
    return state.update(stage="paid", paid_amount=amount)


@action(reads=["stage", "item", "qty"], writes=["stage"])
def fulfill(state: State) -> State:
    """Mark the order as fulfilled. Terminal."""
    return state.update(stage="fulfilled")


@action(reads=["stage"], writes=["stage"])
def cancel(state: State) -> State:
    """Cancel the order. Terminal; only reachable pre-pay."""
    return state.update(stage="cancelled")


def build_application():
    """Build the coffee-order Burr Application."""
    ordered = Condition.expr("stage == 'ordered'")
    paid = Condition.expr("stage == 'paid'")
    return (
        ApplicationBuilder()
        .with_actions(
            take_order=take_order,
            add_modifier=add_modifier,
            pay=pay,
            fulfill=fulfill,
            cancel=cancel,
        )
        .with_transitions(
            # Pay is the default linear path (Burr picks the first
            # matching transition for auto-routing); modifier loop and
            # cancel are alternates the agent reaches via step().
            ("take_order", "pay", ordered),
            ("take_order", "add_modifier", ordered),
            ("take_order", "cancel", ordered),
            ("add_modifier", "pay", ordered),
            ("add_modifier", "add_modifier", ordered),
            ("add_modifier", "cancel", ordered),
            # Post-pay: only fulfill. No refunds.
            ("pay", "fulfill", paid),
        )
        .with_tracker(tracker(project=_TRACKER_PROJECT))
        .with_state(stage="new")
        .with_entrypoint("take_order")
        .build()
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the application as an MCP server."""
    return mount(
        build_application(),
        mode=mode,
        name="coffee-order",
        instructions=(
            "A coffee-order FSM. Walk: take_order(item, qty) -> "
            "[add_modifier(modifier) loop, optional] -> pay(amount) -> "
            "fulfill. The running total is in state.total. cancel "
            "is reachable from any pre-pay state. Read theodosia://state "
            "for the order; theodosia://next for legal next actions."
        ),
    )


if __name__ == "__main__":
    server = build_server()
    server.run()
