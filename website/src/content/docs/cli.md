---
title: 'CLI'
description: 'serve, doctor, render, the observability commands, and build_cli.'
---

`pip install burrmcp` registers a `burrmcp` console script with three groups of
commands: serve, validate, and observe.

## serve and doctor

```bash
burrmcp serve module:attr            # mount an Application or factory as an MCP server
burrmcp serve module:attr --name x   # override the MCP server name
burrmcp doctor module:attr           # static validation before mounting
burrmcp doctor module:attr --runtime # also mount in-process and probe the wire shape
```

The target is a `module:attr` string (the shape uvicorn and gunicorn use). The
attr may be a built `Application` or a callable returning one. `--app-dir`
prepends a directory to `sys.path` before importing; it is repeatable.

`doctor` exits `0` when there are no failures (warnings and info notes do not
block) and `1` otherwise, so it slots into CI. It is importable too:
`from burrmcp.doctor import run_checks`.

## render

`render` draws the mounted state machine without starting a server or needing
Graphviz. It reads the graph statically from the same target `serve` takes.

```bash
burrmcp render module:attr              # terminal view of the graph
burrmcp render module:attr --conditions # show transition conditions on edges
burrmcp render module:attr --mermaid    # emit Mermaid stateDiagram source
burrmcp render module:attr --dot        # emit Graphviz DOT source
```

The default terminal view marks the entry action (`▶`), terminals (`■`), and
self-loops (`↺`), and lists each action's reachable next actions:

```
coffee-order  ·  5 action(s)  ·  entry: take_order
────────────────────────────────────────────────────
 ▶ take_order     → pay · add_modifier · cancel
   add_modifier ↺ → pay · add_modifier · cancel
   pay            → fulfill
 ■ fulfill        (terminal)
 ■ cancel         (terminal)
```

`--mermaid` emits source that GitHub renders inline, so a project can keep its
README diagram in sync with the actual graph instead of hand-drawing it. `--dot`
emits Graphviz DOT for the image-rendering path. Burr's own `Application.visualize()`
covers the Graphviz image output; `render` adds the lightweight, paste-friendly
forms it does not.

## Observability

Every mounted server with a `LocalTrackingClient` writes a per-session JSONL log
under `~/.burr`. These commands read it directly, so they work against any
session, including one running right now in another process.

```bash
burrmcp sessions ls                 # recent sessions, most recent first
burrmcp sessions show <app-id>      # full timeline: per-step state diff + timing
burrmcp sessions tail [app-id]      # live-tail a running session
burrmcp watch [app-id]              # alias for `sessions tail`
burrmcp logs [app-id]               # compact one-line-per-step, greppable
burrmcp logs --refusals --plain     # only steps that errored, pipe-friendly
```

`app-id` defaults to the most-recently-touched session and accepts a uuid prefix.
`--burr-home` points at a tracker root other than `~/.burr`. `--json` on `ls` and
`show` emits machine output.

## ui

```bash
burrmcp ui
```

Launches Burr's web UI. Prefers a local `apache-burr[start]` install; otherwise
bootstraps via `uvx`. Permanent install: `uv pip install 'burrmcp[ui]'`.

## Shipping your own command

`build_cli` returns a Typer app rebranded for a downstream package, with the
graph baked in so `serve`/`doctor` need no target.

```python
# my_fsm_mcp/cli.py
from burrmcp.cli import build_cli, run
from my_fsm_mcp import build_application

cli = build_cli(
    "my-fsm-mcp",
    application=build_application,   # Application, factory, or "module:attr"
    help="My graph as an MCP server.",
    server_name="my-fsm-mcp",           # default MCP server name
    ui_extra="my-fsm-mcp[ui]",          # named in the `ui` install hint
    burr_home="~/.my-fsm-mcp",          # default tracker root for observability
)

def main() -> int:
    return run(cli)
```

```toml
[project.scripts]
my-fsm-mcp = "my_fsm_mcp.cli:main"
```

`run(cli)` wraps the Typer app with the same exit-code handling `burrmcp` uses.

What rebrands: the command name (from the installed console script), the root
help text, the server name, the `ui` install hint, and the default tracker root.
What does not: the on-disk tracker format and the Burr UI, which are Burr's. A
rebranded CLI is a thin shell over burrmcp and Burr, not a fork of them.

`application` is optional. When omitted, the CLI behaves like the default
`burrmcp` and requires a `module:attr` target on `serve`/`doctor`.

To let the rebranded server drive other MCP servers, pass `upstream`. It is
forwarded to `mount`, so action bodies can call `call_upstream(server, tool,
args)`:

```python
cli = build_cli(
    "my-fsm-mcp",
    application=build_application,
    upstream={"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}},
)
```

See [Driving other MCP servers](upstream.md) for the action-body side.
## Drive it from any MCP client

`burrmcp serve module:attr` is a stdio MCP server, so any MCP client can launch
it. The same server is driven by each client through its own config mechanism.
Verified against a fresh `pip install burrmcp`:

```bash
# fast-agent (Python REPL). Define the server in fastagent.config.yaml:
#   mcp: { servers: { vending: { command: burrmcp, args: [serve, vending:build_application] } } }
uvx fast-agent-mcp go --servers vending -m "Buy a soda: choose, pay 2 dollars, dispense."

# Claude Code (reads .mcp.json's mcpServers block):
claude -p "Buy a soda: choose, pay 2 dollars, dispense." \
  --mcp-config .mcp.json --allowedTools mcp__vending__step

# Gemini CLI (reads .gemini/settings.json, same mcpServers schema):
GEMINI_CLI_TRUST_WORKSPACE=true gemini --yolo \
  -p "Buy a soda: choose, pay 2 dollars, dispense."
```

Claude and Gemini share the same `mcpServers` block (command + args), just in
different files (`.mcp.json` vs `.gemini/settings.json`). fast-agent uses its own
`fastagent.config.yaml`. Gemini's headless mode needs
`GEMINI_CLI_TRUST_WORKSPACE=true` to clear its folder-trust gate.

## Multiple graphs

Two ways to serve more than one graph:

- **Separate servers.** Run one `burrmcp serve` per graph. Each is an
  independent MCP server with its own state, history, and `burr://` resources.
  The URI `burr://graph` is identical on every server but is read against a
  specific server connection, so there is no collision; the client namespaces
  tools by server (`mcp__coffee__step` vs `mcp__triage__step`).
- **One server, `mount_multi`.** `mount_multi({"coffee": ..., "triage": ...})`
  composes several graphs into one server. Tools become `coffee_step` /
  `triage_step`, and resources carry the namespace in the URI:
  `burr://coffee/graph`, `burr://triage/next`. A parent `burr://apps` resource
  lists the mounted names.
