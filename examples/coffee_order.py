"""Coffee-order FSM: the smallest example that demonstrates burr-mcp.

Three states, in order:

    take_order -> pay -> fulfill

In strict modes (STEP, DYNAMIC), the server refuses ``pay`` before
``take_order``, and refuses ``fulfill`` before ``pay``. In TOOLS mode,
any action is callable at any time.

Run as a server:

    python examples/coffee_order.py

Inspect from another shell with the FastMCP client or any MCP client.
The ``burr://state`` resource shows the order's current state. The
``burr://next`` resource shows which actions are valid right now.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount

_TRACKER_PROJECT = "coffee-order-demo"


@action(reads=[], writes=["stage", "item", "qty"])
def take_order(state: State, item: str, qty: int = 1) -> State:
    """Place a new coffee order.

    Args:
        item: Drink name, e.g. ``"latte"``, ``"americano"``.
        qty: Number of drinks; defaults to 1.
    """
    return state.update(stage="ordered", item=item, qty=qty)


@action(reads=["stage"], writes=["stage", "paid_amount"])
def pay(state: State, amount: float) -> State:
    """Pay for the placed order.

    Args:
        amount: Payment amount in whatever currency the cafe uses.
    """
    return state.update(stage="paid", paid_amount=amount)


@action(reads=["stage", "item", "qty"], writes=["stage"])
def fulfill(state: State) -> State:
    """Mark the order as fulfilled. Terminal."""
    return state.update(stage="fulfilled")


def build_application():
    """Build the coffee-order Burr Application."""
    return (
        ApplicationBuilder()
        .with_actions(take_order=take_order, pay=pay, fulfill=fulfill)
        .with_transitions(("take_order", "pay"), ("pay", "fulfill"))
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
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
            "A coffee-order FSM. Submit orders via ``take_order``, "
            "then ``pay``, then ``fulfill``. Read ``burr://state`` to "
            "see the order. Read ``burr://next`` to see valid actions."
        ),
    )


if __name__ == "__main__":
    server = build_server()
    server.run()
