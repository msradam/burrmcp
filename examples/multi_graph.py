"""Two graphs in one MCP server via ``mount_multi``.

Most multi-graph setups run one ``burrmcp serve`` per graph: separate
processes, each with its own ``burr://graph``, disambiguated by the client.
``mount_multi`` is the other option: compose several Burr Applications into a
single server. FastMCP namespacing then applies:

* Tools are renamed ``<app>_<tool>``: ``orders_step`` and ``tickets_step``.
* Resources carry the namespace in the URI: ``burr://orders/graph``,
  ``burr://tickets/next``, and so on.
* A parent ``burr://apps`` resource lists the mounted names so an agent can
  discover the surface in one read.

Run it:

    python examples/multi_graph.py

Then drive ``orders_step`` and ``tickets_step`` independently from one client.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount_multi


@action(reads=[], writes=["stage", "item", "qty"])
def place_order(state: State, item: str, qty: int = 1) -> State:
    """Place an order."""
    return state.update(stage="placed", item=item, qty=qty)


@action(reads=["stage"], writes=["stage", "paid"])
def pay(state: State, amount: float) -> State:
    """Pay for the order."""
    return state.update(stage="paid", paid=amount)


@action(reads=["stage"], writes=["stage"])
def ship(state: State) -> State:
    """Ship the order. Terminal."""
    return state.update(stage="shipped")


def build_orders():
    return (
        ApplicationBuilder()
        .with_actions(place_order=place_order, pay=pay, ship=ship)
        .with_transitions(("place_order", "pay"), ("pay", "ship"))
        .with_tracker(LocalTrackingClient(project="multi-graph-orders"))
        .with_state(stage="new")
        .with_entrypoint("place_order")
        .build()
    )


@action(reads=[], writes=["stage", "subject"])
def open_ticket(state: State, subject: str) -> State:
    """Open a support ticket."""
    return state.update(stage="open", subject=subject)


@action(reads=["stage"], writes=["stage", "resolution"])
def resolve(state: State, resolution: str) -> State:
    """Resolve the ticket. Terminal."""
    return state.update(stage="resolved", resolution=resolution)


def build_tickets():
    return (
        ApplicationBuilder()
        .with_actions(open_ticket=open_ticket, resolve=resolve)
        .with_transitions(("open_ticket", "resolve"))
        .with_tracker(LocalTrackingClient(project="multi-graph-tickets"))
        .with_state(stage="new")
        .with_entrypoint("open_ticket")
        .build()
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount both graphs as one MCP server."""
    return mount_multi(
        {"orders": build_orders, "tickets": build_tickets},
        mode=mode,
        name="backoffice",
        instructions=(
            "Two independent workflows on one server. Drive orders with "
            "orders_step and tickets with tickets_step. Read burr://apps for "
            "the list, then burr://orders/graph or burr://tickets/graph for "
            "each topology."
        ),
    )


if __name__ == "__main__":
    build_server().run()
