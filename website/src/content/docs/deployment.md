---
title: Deployment recipes
description: Run a Theodosia-mounted FSM under every common MCP client transport: stdio (Claude Code, Cursor), HTTP/SSE (hosted), Lambda, k8s. One artifact, many deployment shapes.
---

A Theodosia-mounted FSM is a FastMCP server. FastMCP speaks stdio, HTTP, and
SSE; MCP clients pick whichever transport fits their host. The same Burr
Application runs unchanged in every recipe below.

If you came here from Philip with a lifted Ansible playbook, Mermaid
sketch, or Excalidraw diagram, the bindings module `philip` emits drops
straight into recipe 1 (Claude Code, stdio) with no further work.

## Recipe 1: stdio via Claude Code

Claude Code reads `.mcp.json` from the current project root, or
`~/.claude.json` under `mcpServers` for global servers. Both forms accept
the same shape.

```json title=".mcp.json"
{
  "mcpServers": {
    "incident-response": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory", "/abs/path/to/your/project",
        "run", "theodosia", "serve",
        "your_module:build_application"
      ]
    }
  }
}
```

Run `claude` in the project, then `/mcp` confirms the connection.

Examples ship in this repo under `examples/claude-code.example.json`.

## Recipe 2: stdio via Cursor

Cursor reads MCP server config from settings under
`mcp.servers`. The shape mirrors Claude Code:

```json
{
  "mcp.servers": {
    "incident-response": {
      "command": "uv",
      "args": [
        "--directory", "/abs/path/to/your/project",
        "run", "theodosia", "serve",
        "your_module:build_application"
      ]
    }
  }
}
```

Cursor's MCP panel shows the discovered tools and resources once the
server is added.

## Recipe 3: stdio via mcphost

`mcphost` is a CLI MCP client useful for scripted runs and shell
pipelines.

```json title="mcphost.json"
{
  "mcpServers": {
    "incident-response": {
      "command": "uv",
      "args": [
        "--directory", "/abs/path/to/your/project",
        "run", "theodosia", "serve",
        "your_module:build_application"
      ]
    }
  }
}
```

Repo ships `examples/mcphost.example.json` as a starting point.

## Recipe 4: stdio via fast-agent

fast-agent runs an MCP client inside a Python script. The mounted server
is spawned as a subprocess:

```python title="run_agent.py"
import asyncio
from fast_agent import FastAgent

agent = FastAgent(
    mcp_servers={
        "incident-response": {
            "command": "uv",
            "args": [
                "--directory", "/abs/path/to/your/project",
                "run", "theodosia", "serve",
                "your_module:build_application",
            ],
        }
    },
)

async def main():
    async with agent:
        await agent.run("Investigate the cache latency alert.")

asyncio.run(main())
```

## Recipe 5: HTTP transport (hosted on a VM or container)

For hosted deployments where the agent and the server run on different
machines, use HTTP. The server listens on a port; clients connect via
URL.

```bash
theodosia serve your_module:build_application \
  --transport http --host 0.0.0.0 --port 3001
```

Client side (Claude Code with HTTP transport):

```json
{
  "mcpServers": {
    "incident-response": {
      "type": "http",
      "url": "https://your-host.example.com/mcp"
    }
  }
}
```

Stick a reverse proxy (Caddy, nginx, Cloudflare) in front for TLS and
auth.

## Recipe 6: SSE transport

SSE is HTTP with server-sent events for streaming. The launch shape is
the same as HTTP, with `--transport sse`. Some clients (Bob, certain
hosted-LLM gateways) prefer SSE.

```bash
theodosia serve your_module:build_application \
  --transport sse --host 0.0.0.0 --port 3001
```

Client URL takes `sse` instead of `mcp` at the path tail. Check your
client's docs.

## Recipe 7: Lambda + API Gateway

Wrap the server in a Lambda handler that translates API Gateway events
into MCP requests. The FSM stays the same; the wrapper handles the
HTTP envelope.

```python title="lambda_handler.py"
from mangum import Mangum
from fastmcp import FastMCP
import theodosia
from your_module import build_application

server = theodosia.mount(build_application, name="incident-response")
asgi = server.streamable_http_app()
handler = Mangum(asgi)
```

Per-session state lives in the Lambda's filesystem by default; switch
to a persistent Burr `BaseStatePersister` (Redis, DynamoDB) so sessions
survive cold starts.

## Recipe 8: Kubernetes pod (long-running HTTP)

```yaml title="deployment.yaml"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: incident-response-mcp
spec:
  replicas: 2
  selector:
    matchLabels: { app: incident-response-mcp }
  template:
    metadata:
      labels: { app: incident-response-mcp }
    spec:
      containers:
        - name: server
          image: your-registry/incident-response-mcp:latest
          command: ["theodosia", "serve", "your_module:build_application"]
          args: ["--transport", "http", "--host", "0.0.0.0", "--port", "3001"]
          ports:
            - containerPort: 3001
          env:
            - name: THEODOSIA_HOME
              value: "/data/.theodosia"
          volumeMounts:
            - name: tracker
              mountPath: /data
      volumes:
        - name: tracker
          persistentVolumeClaim:
            claimName: incident-response-tracker
```

Use a `PersistentVolumeClaim` so the tracker survives restarts and the
session history stays addressable for replay and reports.

## Recipe 9: Slack / Discord bot (MCP via webhook)

If your bot framework speaks MCP (or has a glue layer that does), the
HTTP recipe applies unchanged. For bots without MCP support, use
`theodosia report --webhook` to push session postmortems into your
incident channel:

```bash
theodosia report \
  --webhook https://hooks.slack.com/services/... \
  $LATEST_APP_ID
```

The report POSTs as `application/markdown` with `X-Theodosia-Project`
and `X-Theodosia-App-Id` headers so Slack workflows can route on them.

## Recipe 10: Embedded as a sub-app

Theodosia exposes `spawn_subapp(...)` for action bodies that need to
fan out into a smaller FSM. The sub-app inherits the parent server's
tracker and tools; no separate deployment needed. See the
[`examples/subgraphs/`](https://github.com/msradam/theodosia/tree/main/examples/subgraphs)
demo for the pattern.

## What carries across every recipe

- The same Burr Application, no code changes between recipes.
- The same `theodosia://...` resources for the agent.
- The same audit trail under `~/.theodosia/<project>/<app_id>/`.
- `theodosia status` and `theodosia report` work against the tracker
  produced by any of these deployments.
- Personas (`PERSONA.md`), classified upstream responses, and recorded
  trajectories all behave identically regardless of transport.

## Same artifact, many shapes (the Philip composition)

If your FSM came from a Philip lift, the deployment chain is:

```
Excalidraw / Mermaid / Ansible / SQL
   -> philip from-X (emits a tiny binding module)
      -> theodosia serve module:app (one of the recipes above)
```

Two libraries, one composition, every transport. The diagram you drew
or the playbook you already had becomes a runnable agent surface
without writing any glue.
