"""HTTP example: serve the coffee FSM over Streamable HTTP.

stdio is the right transport for desktop MCP clients (Claude Desktop,
Cursor) that spawn the server as a subprocess. Streamable HTTP is the
right transport for long-running multi-tenant servers and for any
deployment where the server lives on a different machine than the
clients.

Run:

    python examples/http_serve.py
    # then connect a client to http://127.0.0.1:8765/mcp

The factory form gives each connecting client its own coffee order.
The store keeps Application + history per session, evicting after one
hour idle.
"""

from __future__ import annotations

import os

from coffee_order import build_application

from burr_mcp import ServingMode, mount

HOST = os.environ.get("BURR_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BURR_MCP_PORT", "8765"))


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="coffee-http",
        instructions=(
            "Coffee-order FSM served over HTTP. Each session gets its "
            "own order, isolated from other connected clients."
        ),
    )


if __name__ == "__main__":
    server = build_server()
    server.run(transport="streamable-http", host=HOST, port=PORT)
