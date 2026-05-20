"""Tiny script that walks the coffee FSM through the FastMCP test Client.

Used by ``demo.tape`` to record the README's demo.gif. The script is
deliberately small and prints structured JSON so the recording is
legible at 80-column terminal width.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from coffee_order import build_server  # noqa: E402


def _short(payload: dict) -> str:
    """Compact one-line view of a step response."""
    if payload.get("error"):
        return (
            f'  → refused: {payload["error"]}; '
            f'valid_next_actions={payload.get("valid_next_actions", [])}'
        )
    state = payload.get("state", {})
    keep = {k: state[k] for k in ("stage", "item", "qty", "paid_amount") if k in state}
    return f"  → ok; state={json.dumps(keep)}; next={payload['valid_next_actions']}"


async def main():
    server = build_server()
    async with Client(server) as client:
        steps = [
            ("pay", {"amount": 5.0}),
            ("take_order", {"item": "latte", "qty": 1}),
            ("pay", {"amount": 5.0}),
            ("fulfill", {}),
        ]
        for action, inputs in steps:
            print(f"step(action={action!r}, inputs={inputs})")
            r = await client.call_tool("step", {"action": action, "inputs": inputs})
            print(_short(json.loads(r.content[0].text)))
            print()


if __name__ == "__main__":
    asyncio.run(main())
