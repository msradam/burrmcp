---
title: 'Driving other MCP servers'
description: 'The upstream feature: actions calling other MCP servers.'
---

Theodosia is normally the MCP server the agent talks to. With `upstream`, it also
opens MCP *client* sessions to other servers (Kubernetes, Grafana, filesystem,
and so on). A Burr action calls those servers' tools from inside its Python body
via `call_upstream(server, tool, args)`.

## Wiring

```python
from theodosia import call_upstream, mount
from burr.core import action

@action(reads=[], writes=["pods"])
async def survey(state):
    pods = await call_upstream("k8s", "list_pods", {"namespace": "prod"})
    return state.update(pods=pods)

server = mount(
    build_application,
    upstream={"k8s": {"command": "npx", "args": ["-y", "kubernetes-mcp-server"]}},
)
```

Each value in the `upstream` map is anything `fastmcp.Client` accepts as a
transport: a URL string, an mcp-config dict, a transport object, or a bare
`{"command", "args", "env", "cwd"}` stdio spec. The bare stdio spec is mapped to
an explicit `StdioTransport` so the upstream tool names are not namespaced the
way an mcp-config dict would prefix them.

## Why this shape

- **Single surface.** The agent connects to one server (this one) and sees one
  tool (`step`). The upstream servers are never exposed to it. There is no
  separate "query the cluster" surface for a weak model to get absorbed in.
- **Every call is a ledger entry.** The upstream call happens inside an action,
  so it advances state by construction. The graph cannot fall out of sync with
  what actually happened.
- **Any server.** MCP is a standard protocol and `fastmcp.Client` speaks every
  transport (stdio, http, sse). Theodosia does not need to know what the upstream
  server is.
- **No arg-guessing.** The action author writes the call explicitly. There is no
  per-backend name or argument inference.

## Lifecycle

`UpstreamManager` lazily opens and caches one `fastmcp.Client` session per
server, keyed by name, opened on first use and kept open for the manager's
lifetime. Calls are serialized per manager with an `asyncio.Lock`, since a single
Client session is not guaranteed safe under concurrent calls and Burr steps are
serialized per session anyway. `mount()` binds a manager around each `step` via
`bind_upstream` and resets it afterward.

For tests or harness embeddings, `bind_upstream` accepts any object with an async
`call(server, tool, args)` method, so you can bind an already-open session
instead of the built-in manager.

## Example

`examples/upstream_filesystem.py` is a code-audit FSM that drives the official
filesystem MCP server this way: list files, read a candidate, flag findings,
report. The agent only ever calls `step`.

## Testing with `FakeUpstream`

`theodosia.testing.FakeUpstream` is an in-process stand-in for upstream MCP
servers. It satisfies the same `async call(server, tool, args)` protocol as
`UpstreamManager`, so a test passes it where a real upstream config would go.
Every call is recorded for later assertion.

```python
from theodosia.testing import FakeUpstream
from theodosia import mount

fake = FakeUpstream({
    "grafana": {
        "list_datasources": [{"name": "prometheus", "type": "prometheus"}],
        "query_metric": lambda args: {"rate": 0.42, "series": args["query"]},
    },
})

server = mount(build_application, name="incident", upstream=fake)
# ... drive the server through an MCP Client; the actions reach `fake` instead
# of touching the network.

assert fake.calls_to("grafana", "query_metric")[0].args["query"] == "rate(http_requests[5m])"
```

Responses can be static values, sync callables, or async callables taking the
args dict. A callable that raises simulates upstream failure, which surfaces
through `safe_upstream` as a classified `ERROR` result.

For trajectory-based tests, `theodosia.testing.RecordingUpstream` wraps a real
upstream and writes every call to a JSONL fixture; `ReplayingUpstream` serves
that fixture back in order, raising `ReplayMismatch` if a drift call arrives.
Useful for "record once against the real server, replay forever" test patterns.