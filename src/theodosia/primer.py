"""``theodosia primer``: 30-second offline first-touch.

The first thing to run. No API key, no setup, no LLM. Walks a fixed
trajectory through ``examples/coffee_order.py`` in-process via FastMCP's
in-memory client, prints the timeline with state diffs, then provokes one
structured refusal so the recoverable shape is visible before the reader
ever stands up an MCP client.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "examples"

_TRAJECTORY: list[tuple[str, dict[str, Any]]] = [
    ("take_order", {"item": "latte", "qty": 1}),
    ("add_modifier", {"modifier": "oat_milk"}),
    ("add_modifier", {"modifier": "extra_shot"}),
    ("pay", {"amount": 7.0}),
    ("fulfill", {}),
]

_REFUSAL_PROBE: tuple[str, dict[str, Any]] = ("take_order", {"item": "americano"})


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
    if str(_EXAMPLES_DIR) not in sys.path:
        sys.path.insert(0, str(_EXAMPLES_DIR))

    try:
        import coffee_order
    except ImportError:
        console.print(
            "[red]error[/red]: bundled coffee_order example not found at "
            f"{_EXAMPLES_DIR}. Quickstart requires a source checkout."
        )
        return 1

    from fastmcp import Client

    from theodosia import mount

    server = mount(coffee_order.build_application, name="primer")

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
        for seq, (action, inputs) in enumerate(_TRAJECTORY):
            r = await client.call_tool("step", {"action": action, "inputs": inputs})
            out = r.structured_content or {}
            new_state = dict(out.get("state") or {})
            diff = _diff_state(last_state, new_state) or "(no change)"
            table.add_row(str(seq), action, _format_inputs(inputs), "OK", diff)
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
    console.print(
        "  [cyan]theodosia serve coffee_order:build_application --app-dir examples[/cyan]"
    )
    console.print(
        "  [cyan]theodosia render coffee_order:build_application --app-dir examples[/cyan]"
    )
    console.print(
        "  [cyan]theodosia doctor coffee_order:build_application --app-dir examples[/cyan]"
    )
    console.print()
    return 0


def primer() -> None:
    """Run the 30-second offline first-touch."""
    console = Console()
    code = asyncio.run(_run(console))
    if code:
        raise SystemExit(code)
