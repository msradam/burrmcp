"""SSE example: serve the coffee FSM over Server-Sent Events.

Streamable HTTP (see ``http_serve.py``) is the modern default for
multi-tenant HTTP deployments. SSE remains in use for clients that
predate the streamable-http transport or that interoperate with
SSE-only proxies. The serving code is identical; only the
``transport=`` argument to ``server.run()`` changes.

Run:

    python examples/sse_serve.py
    # then connect a client to http://127.0.0.1:8766/sse

Configure via env vars: ``BURR_MCP_HOST``, ``BURR_MCP_PORT``.
"""

from __future__ import annotations

import os

from coffee_order import build_application

from burrmcp import ServingMode, mount

HOST = os.environ.get("BURR_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BURR_MCP_PORT", "8766"))


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="coffee-sse",
        instructions="Coffee-order FSM served over SSE.",
    )


if __name__ == "__main__":
    build_server().run(transport="sse", host=HOST, port=PORT)
