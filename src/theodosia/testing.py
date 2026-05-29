"""Testing primitives: ``FakeUpstream`` plus recording/replay helpers.

``FakeUpstream`` is an in-process stand-in for upstream MCP servers,
satisfying the same ``async call(server, tool, args)`` protocol as the
real ``UpstreamManager``. Pass it to ``theodosia.mount(..., upstream=fake)``
in tests instead of standing up real MCP servers.

Responses can be static values, sync callables, or async callables taking
the args dict. A callable that raises simulates upstream failure.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from theodosia._recording import (
    RecordingUpstream as RecordingUpstream,
)
from theodosia._recording import (
    ReplayingUpstream as ReplayingUpstream,
)
from theodosia._recording import (
    ReplayMismatch as ReplayMismatch,
)

ToolResponse = Any | Callable[[dict[str, Any]], Any] | Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class FakeCall:
    """One recorded call to a :class:`FakeUpstream` tool."""

    server: str
    tool: str
    args: dict[str, Any]


class FakeUpstream:
    """In-process fake of one or more upstream MCP servers.

    Satisfies the ``UpstreamManager`` protocol so it can be passed as
    ``mount(..., upstream=fake)``. Every call is recorded.
    """

    def __init__(
        self,
        responses: dict[str, dict[str, ToolResponse]] | None = None,
    ) -> None:
        self._responses: dict[str, dict[str, ToolResponse]] = {
            server: tools.copy() for server, tools in (responses or {}).items()
        }
        self._calls: list[FakeCall] = []
        self._lock = asyncio.Lock()

    @property
    def calls(self) -> list[FakeCall]:
        """All recorded calls in order. Returns a copy so tests can mutate."""
        return self._calls.copy()

    @property
    def server_names(self) -> list[str]:
        return sorted(self._responses)

    def calls_to(self, server: str, tool: str | None = None) -> list[FakeCall]:
        """Filter the recorded calls by server, optionally by tool."""
        if tool is None:
            return [c for c in self._calls if c.server == server]
        return [c for c in self._calls if c.server == server and c.tool == tool]

    def clear(self) -> None:
        """Drop all recorded calls; useful between phases of a test."""
        self._calls.clear()

    def register(self, server: str, tool: str, response: ToolResponse) -> None:
        """Add or replace a tool response after construction."""
        self._responses.setdefault(server, {})[tool] = response

    # ── Manager protocol ──────────────────────────────────────────────────

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        """Record the call and return the registered response."""
        from theodosia.upstream import UpstreamError

        async with self._lock:
            self._calls.append(FakeCall(server=server, tool=tool, args=args.copy()))
            if server not in self._responses:
                raise UpstreamError(
                    f"FakeUpstream: unknown server {server!r}; registered: {self.server_names}"
                )
            if tool not in self._responses[server]:
                raise UpstreamError(
                    f"FakeUpstream: unknown tool {tool!r} on {server!r}; "
                    f"registered: {sorted(self._responses[server])}"
                )
            spec = self._responses[server][tool]

        if callable(spec):
            result = spec(args)
            if inspect.isawaitable(result):
                result = await result
            return result
        return spec

    async def aclose(self) -> None:
        """No-op; provided to match UpstreamManager's surface."""


__all__ = [
    "FakeCall",
    "FakeUpstream",
    "RecordingUpstream",
    "ReplayMismatch",
    "ReplayingUpstream",
]
