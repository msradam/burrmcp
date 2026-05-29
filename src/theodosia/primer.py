"""``theodosia primer``: 30-second offline first-touch.

The first thing to run after ``pip install``. Mounts a self-contained
coffee-order FSM in-process via FastMCP's in-memory client, walks a fixed
trajectory, then provokes one structured refusal so the recoverable shape is
visible before the reader ever stands up an MCP client.

No external example file is required. The FSM is defined in this module so
the command works from a wheel install.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from burr.core import ApplicationBuilder, Condition, State, action
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_BASE_PRICE = 5.0
_MODIFIER_PRICE = {"extra_shot": 1.0, "oat_milk": 1.0, "syrup": 1.0}

_TRAJECTORY: list[tuple[str, dict[str, Any]]] = [
    ("take_order", {"item": "latte", "qty": 1}),
    ("add_modifier", {"modifier": "oat_milk"}),
    ("add_modifier", {"modifier": "extra_shot"}),
    ("pay", {"amount": 7.0}),
    ("fulfill", {}),
]

_REFUSAL_PROBE: tuple[str, dict[str, Any]] = ("take_order", {"item": "americano"})


@action(reads=[], writes=["stage", "item", "qty", "modifiers", "total"])
def _take_order(state: State, item: str, qty: int = 1) -> State:
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
def _add_modifier(
    state: State,
    modifier: Literal["extra_shot", "oat_milk", "syrup"],
) -> State:
    return state.update(
        modifiers=[*state["modifiers"], modifier],
        total=state["total"] + _MODIFIER_PRICE[modifier],
    )


@action(reads=["stage"], writes=["stage", "paid_amount"])
def _pay(state: State, amount: float) -> State:
    return state.update(stage="paid", paid_amount=amount)


@action(reads=["stage"], writes=["stage"])
def _fulfill(state: State) -> State:
    return state.update(stage="fulfilled")


@action(reads=["stage"], writes=["stage"])
def _cancel(state: State) -> State:
    return state.update(stage="cancelled")


def _build_primer_application():
    ordered = Condition.expr("stage == 'ordered'")
    paid = Condition.expr("stage == 'paid'")
    return (
        ApplicationBuilder()
        .with_actions(
            take_order=_take_order,
            add_modifier=_add_modifier,
            pay=_pay,
            fulfill=_fulfill,
            cancel=_cancel,
        )
        .with_transitions(
            ("take_order", "pay", ordered),
            ("take_order", "add_modifier", ordered),
            ("take_order", "cancel", ordered),
            ("add_modifier", "pay", ordered),
            ("add_modifier", "add_modifier", ordered),
            ("add_modifier", "cancel", ordered),
            ("pay", "fulfill", paid),
        )
        .with_state(stage="new")
        .with_entrypoint("take_order")
        .build()
    )


def _format_inputs(inputs: dict[str, Any]) -> str:
    if not inputs:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in inputs.items())


def _diff_state(before: dict[str, Any], after: dict[str, Any]) -> str:
    changed: list[str] = []
    for k, v in after.items():
        if before.get(k) != v:
            changed.append(f"{k}={v!r}")
    return ", ".join(changed)


async def _run(console: Console) -> int:
    for noisy in ("fastmcp", "mcp", "FastMCP", "FastMCP.fastmcp.server.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from fastmcp import Client

    from theodosia import mount

    server = mount(_build_primer_application, name="primer")

    console.print()
    console.print(
        Panel.fit(
            "[bold]theodosia primer[/bold]\n"
            "Walks the coffee-order FSM through Theodosia's step tool.\n"
            "No API key, no LLM, byte-deterministic.",
            border_style="magenta",
        )
    )

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("seq", justify="right", style="dim", width=4)
    table.add_column("action", style="cyan", width=18)
    table.add_column("inputs", style="white", width=28)
    table.add_column("result", style="green", width=6)
    table.add_column("state change", style="white")

    last_state: dict[str, Any] = {}
    async with Client(server) as client:
        for seq, (action_name, inputs) in enumerate(_TRAJECTORY):
            r = await client.call_tool("step", {"action": action_name, "inputs": inputs})
            out = r.structured_content or {}
            new_state = dict(out.get("state") or {})
            diff = _diff_state(last_state, new_state) or "(no change)"
            table.add_row(str(seq), action_name, _format_inputs(inputs), "OK", diff)
            last_state = new_state

        seq = len(_TRAJECTORY)
        probe_action, probe_inputs = _REFUSAL_PROBE
        r = await client.call_tool("step", {"action": probe_action, "inputs": probe_inputs})
        out = r.structured_content or {}
        err = out.get("error", "unknown")
        reachable = out.get("valid_next_actions") or []
        reachable_text = ", ".join(reachable) if reachable else "(terminal)"
        table.add_row(
            str(seq),
            probe_action,
            _format_inputs(probe_inputs),
            "[red]REFUSE[/red]",
            f"{err}; reachable: {reachable_text}",
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "The refusal carries [bold]valid_next_actions[/bold]. An LLM agent reads that "
        "and self-corrects, no retry prompt needed."
    )
    console.print()
    console.print("[bold]Next steps[/bold]")
    console.print("  Read the docs at https://msradam.github.io/theodosia/")
    console.print("  Author your own graph: [cyan]theodosia doctor my_module:build[/cyan]")
    console.print("  Mount it: [cyan]theodosia serve my_module:build[/cyan]")
    console.print()
    return 0


def primer() -> None:
    """Run the 30-second offline first-touch."""
    console = Console()
    code = asyncio.run(_run(console))
    if code:
        raise SystemExit(code)
