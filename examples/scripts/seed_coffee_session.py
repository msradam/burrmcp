"""Seed a coffee-order session for the observability demos.

Walks the coffee_order FSM through a refusal (pay before ordering) and a
recovery (order, add modifiers, pay, fulfill), so `burrmcp sessions show`
/ `logs` / `watch` have a session with both a red refusal row and green
success rows to render.

    uv run python examples/scripts/seed_coffee_session.py          # fast
    uv run python examples/scripts/seed_coffee_session.py --slow   # paced for `watch`
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coffee_order import build_server
from fastmcp import Client

_SLOW = "--slow" in sys.argv


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


async def main() -> None:
    server = build_server()
    async with Client(server) as client:
        # Refusal: order with an invalid qty -> action_error (red row).
        # (action_error is recorded by Burr's tracker; invalid_transition,
        # refused before the action runs, lives only in burr://history.)
        await _step(client, "take_order", item="latte", qty=0)
        if _SLOW:
            await asyncio.sleep(2)
        # Recovery: order, add modifiers, pay, fulfill.
        for action, inputs in [
            ("take_order", {"item": "latte", "qty": 1}),
            ("add_modifier", {"modifier": "extra_shot"}),
            ("add_modifier", {"modifier": "oat_milk"}),
            ("pay", {"amount": 7}),
            ("fulfill", {}),
        ]:
            await _step(client, action, **inputs)
            if _SLOW:
                await asyncio.sleep(2)
    print("seeded coffee-order-demo session")


if __name__ == "__main__":
    asyncio.run(main())
