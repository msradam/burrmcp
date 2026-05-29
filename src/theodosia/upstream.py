"""Upstream MCP servers: theodosia as an MCP client to other servers.

``bind_upstream`` installs a manager for the current context; ``call_upstream``
invokes a tool on a named upstream from inside a Burr action body. A manager
is anything with an async ``call(server, tool, args)`` method.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_UPSTREAM: ContextVar[Any | None] = ContextVar("theodosia_upstream", default=None)


class UpstreamError(RuntimeError):
    """An upstream call failed or no manager/server was available."""


# ── Classified upstream responses ─────────────────────────────────────────

OK = "ok"
ERROR = "error"
MALFORMED = "malformed"

_STATUS_VALUES = frozenset({OK, ERROR, MALFORMED})
_ERROR_TEXT_HINTS = ("error", "exception", "traceback", "failed")
_DETAIL_LIMIT = 300


@dataclass
class SourceResult:
    """A classified upstream response: OK, ERROR, or MALFORMED."""

    name: str
    status: str
    data: Any = None
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _STATUS_VALUES:
            raise ValueError(
                f"SourceResult.status must be one of {sorted(_STATUS_VALUES)}; got {self.status!r}"
            )

    @property
    def usable(self) -> bool:
        """True when the data is structured and not error-shaped."""
        return self.status == OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "data": self.data,
            "detail": self.detail,
            "meta": self.meta,
        }


def classify_payload(name: str, payload: Any, *, expect: str = "any") -> SourceResult:
    """Classify a returned payload. ``expect`` is one of ``any``, ``list``, ``dict``."""
    if payload is None:
        return SourceResult(name, ERROR, detail="empty response")
    if isinstance(payload, str):
        low = payload.lower()
        if any(hint in low for hint in _ERROR_TEXT_HINTS):
            return SourceResult(name, ERROR, detail=payload[:_DETAIL_LIMIT])
        return SourceResult(name, MALFORMED, data=payload, detail="unstructured text")
    if isinstance(payload, dict) and payload.get("error"):
        return SourceResult(name, ERROR, detail=str(payload.get("error"))[:_DETAIL_LIMIT])
    if expect == "list" and not isinstance(payload, (list, dict)):
        return SourceResult(name, MALFORMED, data=payload, detail="expected list/dict")
    if expect == "dict" and not isinstance(payload, dict):
        return SourceResult(name, MALFORMED, data=payload, detail="expected dict")
    return SourceResult(name, OK, data=payload)


async def safe_upstream(
    name: str,
    server: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    expect: str = "any",
) -> SourceResult:
    """Call an upstream tool and return a classified result. Never raises."""
    try:
        payload = await call_upstream(server, tool, args or {})
    except UpstreamError as exc:
        return SourceResult(name, ERROR, detail=f"upstream unavailable: {exc}"[:_DETAIL_LIMIT])
    except Exception as exc:
        return SourceResult(name, ERROR, detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT])
    return classify_payload(name, payload, expect=expect)


def coverage(results: list[SourceResult]) -> tuple[int, int]:
    """Return ``(usable_sources, configured_sources)``."""
    return sum(1 for r in results if r.usable), len(results)


def confidence_label(usable: int, total: int) -> str:
    """Map a coverage tuple to ``none``, ``degraded``, or ``full``."""
    if 0 in (total, usable):
        return "none"
    if usable < total:
        return "degraded"
    return "full"


def bind_upstream(manager: Any):
    """Bind an upstream manager for the current context; returns the reset token."""
    return _UPSTREAM.set(manager)


def reset_upstream(token) -> None:
    _UPSTREAM.reset(token)


async def call_upstream(server: str, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call ``tool`` on upstream ``server``; raises ``UpstreamError`` if unbound."""
    mgr = _UPSTREAM.get()
    if mgr is None:
        raise UpstreamError(
            "No upstream manager bound. mount(application, upstream={...}) wires "
            "upstream MCP servers; outside mount, bind one with bind_upstream(...)."
        )
    return await mgr.call(server, tool, args or {})


def _extract(result: Any) -> Any:
    """Pull a JSON-able payload out of an MCP CallToolResult."""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc
    content = getattr(result, "content", None)
    if content:
        parts = [t for c in content if (t := getattr(c, "text", None))]
        if parts:
            text = "\n".join(parts)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return None


def _as_transport(config: Any) -> Any:
    """Map an upstream config to a ``fastmcp.Client`` transport.

    A bare ``{"command": ..., "args": [...]}`` becomes a ``StdioTransport``
    so upstream tool names are not namespaced the way an mcp-config dict
    would prefix them.
    """
    if isinstance(config, dict) and "command" in config and "mcpServers" not in config:
        from fastmcp.client.transports import StdioTransport

        return StdioTransport(
            command=config["command"],
            args=list(config.get("args") or []),
            env=config.get("env"),
            cwd=config.get("cwd"),
        )
    return config


class UpstreamManager:
    """Lazily opens and caches one ``fastmcp.Client`` session per upstream server."""

    def __init__(self, configs: dict[str, Any]):
        self._configs = configs.copy()
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def server_names(self) -> list[str]:
        return sorted(self._configs)

    async def _client(self, server: str):
        if server in self._clients:
            return self._clients[server]
        if server not in self._configs:
            raise UpstreamError(
                f"unknown upstream server {server!r}; configured: {self.server_names}"
            )
        from fastmcp import Client

        client = Client(_as_transport(self._configs[server]))
        try:
            await client.__aenter__()
        except RuntimeError as exc:
            if "fileno" in str(exc):
                raise UpstreamError(
                    f"upstream {server!r} (stdio subprocess) cannot connect from an "
                    "in-memory FastMCP Client. Either drive the parent server over "
                    "stdio/http/sse (any real transport), or substitute "
                    "``theodosia.testing.FakeUpstream`` for unit tests."
                ) from exc
            raise
        self._clients[server] = client
        return client

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        async with self._lock:
            client = await self._client(server)
            result = await client.call_tool(tool, args)
        return _extract(result)

    async def aclose(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                with contextlib.suppress(Exception):
                    await client.__aexit__(None, None, None)
            self._clients.clear()
