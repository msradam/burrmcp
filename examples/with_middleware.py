"""FastMCP middleware wired around a mounted Theodosia server.

FastMCP ships drop-in middleware for the common cross-cutting concerns:
``TimingMiddleware`` (per-request wall-clock), ``LoggingMiddleware``
(structured logs), ``RateLimitingMiddleware`` (token-bucket per session
/ globally), ``ErrorHandlingMiddleware`` (uniform exception envelopes),
plus auth, caching, and response limiting. They attach to any FastMCP
server via ``mcp.add_middleware(...)`` and fire around every request
the server handles.

A Theodosia-mounted server is just a FastMCP server, so anything FastMCP
ships works through it. This demo:

* Mounts a tiny counter FSM via ``mount(...)``.
* Attaches ``TimingMiddleware``, a custom ``StructuredLoggingMiddleware``
  with JSON output, and a permissive ``RateLimitingMiddleware``.
* Adds a small custom middleware that records per-tool counts into an
  in-memory dict, exposed via a custom ``theodosia://tool-counts`` resource
  so you can read the middleware's view of the world from the agent
  side.

The custom counts let tests verify the middleware actually fires;
the prebuilt middleware classes are validated by the FastMCP test
suite, so we just wire them and trust them.

Run:

    uv run python examples/with_middleware.py
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "with-middleware-demo"


# == actions =========================================================


@action(reads=["count"], writes=["count"])
async def tick(state: State) -> State:
    """Increment the counter."""
    return state.update(count=state["count"] + 1)


@action(reads=[], writes=["count"])
async def reset(state: State) -> State:
    """Zero the counter."""
    return state.update(count=0)


# == custom middleware: tool-call counter ============================


class _ToolCallCounter(Middleware):
    """Records per-tool-name call counts. Exposed via theodosia://tool-counts.

    Demonstrates the ``on_call_tool`` hook; FastMCP also provides
    ``on_request`` (around every request), ``on_message`` (around
    every JSON-RPC message), ``on_read_resource``, ``on_get_prompt``,
    etc. See ``fastmcp.server.middleware.Middleware`` for the full
    set of override points.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool_name = getattr(context.message, "name", "unknown")
        self.counts[tool_name] += 1
        return await call_next(context)

    def snapshot(self) -> dict[str, int]:
        return dict(self.counts)


# == graph ===========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(tick=tick, reset=reset)
        .with_transitions(
            ("tick", "reset", Condition.expr("count > 0")),
            ("tick", "tick"),
            ("reset", "tick"),
        )
        .with_state(count=0)
        .with_entrypoint("tick")
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .build()
    )


def build_server(*, rate_limit_per_second: float = 100.0):
    """Build a mounted server with four middlewares attached.

    The middlewares fire in registration order on the request path
    and reverse order on the response path. Order matters for some
    combinations (e.g. rate limiting before timing means timed
    requests include the throttle wait; after means they don't).
    """
    server = mount(
        build_application,
        mode=ServingMode.STEP,
        name="with-middleware",
        instructions=(
            "Counter FSM (tick / reset) instrumented with FastMCP "
            "middleware: timing, structured JSON logging, rate "
            "limiting, plus a per-tool counter. Read "
            "theodosia://tool-counts to see the custom middleware's "
            "tally; check stderr for the timing + logging output."
        ),
    )

    counter = _ToolCallCounter()
    # Order: rate limit first so the timing measurement includes the
    # throttle pause; logging last so it sees the final result shape.
    server.add_middleware(RateLimitingMiddleware(max_requests_per_second=rate_limit_per_second))
    server.add_middleware(TimingMiddleware(logger=logging.getLogger("with_middleware.timing")))
    server.add_middleware(counter)
    server.add_middleware(
        StructuredLoggingMiddleware(
            logger=logging.getLogger("with_middleware.access"),
            include_payloads=False,
        )
    )

    @server.resource("theodosia://tool-counts")
    async def _tool_counts_resource() -> str:
        return json.dumps(counter.snapshot(), indent=2)

    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_server().run()
