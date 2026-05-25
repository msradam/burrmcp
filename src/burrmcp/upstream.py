"""Upstream MCP servers: burrmcp as an MCP *client* to other servers.

This is the load-bearing half of the "works with any MCP server" promise.
burrmcp is normally the MCP *server* the agent talks to. With ``upstream``,
it also opens MCP *client* sessions to other servers (Kubernetes, Grafana,
filesystem, ...). A Burr action can then call those servers' tools from
inside its Python body via ``call_upstream(server, tool, args)``.

Why this keeps the architecture honest:

* **Single surface.** The agent only ever sees burrmcp's ``step`` tool.
  The upstream servers are not exposed to it. There is no separate "query
  the cluster" surface to get absorbed in, so weak models can't loop.
* **Every call is a ledger entry.** The upstream call happens inside an
  action, so it advances state by construction. The graph can't fall out
  of sync with what actually happened.
* **Any server.** MCP is a standard protocol and ``fastmcp.Client``
  speaks every transport (stdio, http, sse). burrmcp doesn't need to know
  what the upstream server is.
* **No arg-guessing.** The action author writes the call explicitly
  (server, tool, args) -- the same as calling any API. No fragile
  per-backend name/arg inference.

Bind a manager with ``bind_upstream`` (mount does this around each step);
call tools with ``call_upstream``. Tests/embeddings can bind any object
with an async ``call(server, tool, args)`` method.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextvars import ContextVar
from typing import Any

_UPSTREAM: ContextVar[Any | None] = ContextVar("burrmcp_upstream", default=None)


class UpstreamError(RuntimeError):
    """An upstream call failed or no manager/server was available."""


def bind_upstream(manager: Any):
    """Bind an upstream manager for the current context. Returns the token
    for ``_UPSTREAM.reset(token)``. ``manager`` is anything with an async
    ``call(server, tool, args) -> Any`` method (the built-in
    ``UpstreamManager`` or a custom one, e.g. a harness wrapping an
    already-open session)."""
    return _UPSTREAM.set(manager)


def reset_upstream(token) -> None:
    _UPSTREAM.reset(token)


async def call_upstream(server: str, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call ``tool`` on the upstream MCP ``server`` with ``args``.

    Returns the tool's result (structured content if present, else text).
    Raises ``UpstreamError`` if no manager is bound or the server is
    unknown. Call this from inside a Burr action body.
    """
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
    """Map an upstream config to something ``fastmcp.Client`` accepts.

    A bare ``{"command": ..., "args": [...]}`` dict becomes an explicit
    ``StdioTransport`` (so the upstream tool names are NOT namespaced, the
    way an mcp-config dict would prefix them). Everything else (URL string,
    mcp-config dict, transport object, FastMCP instance) is passed through
    untouched.
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
    """Lazily opens and caches ``fastmcp.Client`` sessions to upstream
    servers, keyed by name. One session per server, opened on first use,
    kept open for the manager's lifetime.

    ``configs`` maps a server name to anything ``fastmcp.Client`` accepts
    as its transport: a URL string, an mcp-config dict, a transport object.
    """

    def __init__(self, configs: dict[str, Any]):
        self._configs = dict(configs or {})
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
        await client.__aenter__()  # keep the session open across calls
        self._clients[server] = client
        return client

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        # Serialize per-manager: a single Client session isn't guaranteed
        # safe under concurrent calls, and Burr steps are serialized per
        # session anyway.
        async with self._lock:
            client = await self._client(server)
            result = await client.call_tool(tool, args or {})
        return _extract(result)

    async def aclose(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                with contextlib.suppress(Exception):
                    await client.__aexit__(None, None, None)
            self._clients.clear()
