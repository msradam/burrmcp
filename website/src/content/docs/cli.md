---
title: 'CLI'
description: 'serve, doctor, render, the observability commands, and build_cli.'
---

`pip install theodosia` registers a `theodosia` console script with four groups of
commands: first-touch, serve, validate, and observe.

## primer

`primer` is the 30-second offline first-touch. It mounts the bundled
[`coffee_order`](https://github.com/msradam/theodosia/tree/main/examples/coffee_order.py)
FSM in-process via FastMCP's in-memory client, walks a fixed trajectory through
the `step` tool, prints the timeline with state diffs, and ends with one
structured refusal so the recoverable shape is visible.

```bash
theodosia primer
```

No API key, no LLM, byte-deterministic. The first thing to run after `pip install`.

## serve and doctor

```bash
theodosia serve module:attr            # mount an Application or factory as an MCP server
theodosia serve module:attr --name x   # override the MCP server name
theodosia doctor module:attr           # static validation before mounting
theodosia doctor module:attr --runtime # also mount in-process and probe the wire shape
```

The target is a `module:attr` string (the shape uvicorn and gunicorn use). The
attr may be a built `Application` or a callable returning one. `--app-dir`
prepends a directory to `sys.path` before importing; it is repeatable.

`doctor` exits `0` when there are no failures (warnings and info notes do not
block) and `1` otherwise, so it slots into CI. It is importable too:
`from theodosia.doctor import run_checks`.

## render

`render` draws the mounted state machine without starting a server or needing
Graphviz. It reads the graph statically from the same target `serve` takes.

```bash
theodosia render module:attr              # terminal view of the graph
theodosia render module:attr --conditions # show transition conditions on edges
theodosia render module:attr --mermaid    # emit Mermaid stateDiagram source
theodosia render module:attr --dot        # emit Graphviz DOT source
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
theodosia sessions ls                 # recent sessions, most recent first
theodosia sessions show <app-id>      # full timeline: per-step state diff + timing
theodosia sessions tail [app-id]      # live-tail a running session
theodosia watch [app-id]              # alias for `sessions tail`
theodosia logs [app-id]               # compact one-line-per-step, greppable
theodosia logs --refusals --plain     # only steps that errored, pipe-friendly
theodosia verify [app-id]             # check the tamper-evident ledger; nonzero if broken
```

`theodosia verify` recomputes the session's hash-chained `ledger.jsonl` (written
next to the tracker log, one entry per step and refusal) and names the exact line
if anything was edited, reordered, or deleted after the fact. It exits nonzero on
tampering, so it drops into CI or a cron audit. See the
[security model](security-model.md) for what the chain does and does not prove.

`app-id` defaults to the most-recently-touched session and accepts a uuid prefix.
`--home` points at a tracker root other than the default `~/.theodosia` (Burr's
own `LocalTrackingClient` writes to `~/.burr`, so pass `--home ~/.burr` for a
graph wired that way). `--json` on `ls` and `show` emits machine output.

## ui

```bash
theodosia ui
```

Launches Burr's web UI. Prefers a local `apache-burr[start]` install; otherwise
bootstraps via `uvx`. Permanent install: `uv pip install 'theodosia[ui]'`.

## Shipping your own command

`build_cli` returns a Typer app rebranded for a downstream package, with the
graph baked in so `serve`/`doctor` need no target.

```python
# my_fsm_mcp/cli.py
from theodosia.cli import build_cli, run
from my_fsm_mcp import build_application

cli = build_cli(
    "my-fsm-mcp",
    application=build_application,   # Application, factory, or "module:attr"
    help="My graph as an MCP server.",
    server_name="my-fsm-mcp",           # default MCP server name
    ui_extra="my-fsm-mcp[ui]",          # named in the `ui` install hint
    home="~/.my-fsm-mcp",               # default tracker root for observability
)

def main() -> int:
    return run(cli)
```

```toml
[project.scripts]
my-fsm-mcp = "my_fsm_mcp.cli:main"
```

`run(cli)` wraps the Typer app with the same exit-code handling `theodosia` uses.

What rebrands: the command name (from the installed console script), the root
help text, the server name, the `ui` install hint, and the default tracker root.
What does not: the on-disk tracker format and the Burr UI, which are Burr's. A
rebranded CLI is a thin shell over theodosia and Burr, not a fork of them.

`application` is optional. When omitted, the CLI behaves like the default
`theodosia` and requires a `module:attr` target on `serve`/`doctor`.

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

`theodosia serve module:attr` is a stdio MCP server, so any MCP client can launch
it. The same server is driven by each client through its own config mechanism.
Verified against a fresh `pip install theodosia`:

```bash
# fast-agent (Python REPL). Define the server in fastagent.config.yaml:
#   mcp: { servers: { vending: { command: theodosia, args: [serve, vending:build_application] } } }
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

- **Separate servers.** Run one `theodosia serve` per graph. Each is an
  independent MCP server with its own state, history, and `theodosia://` resources.
  The URI `theodosia://graph` is identical on every server but is read against a
  specific server connection, so there is no collision; the client namespaces
  tools by server (`mcp__coffee__step` vs `mcp__triage__step`).
- **One server, `mount_multi`.** `mount_multi({"coffee": ..., "triage": ...})`
  composes several graphs into one server. Tools become `coffee_step` /
  `triage_step`, and resources carry the namespace in the URI:
  `theodosia://coffee/graph`, `theodosia://triage/next`. A parent `theodosia://apps` resource
  lists the mounted names. See `examples/multi_graph.py`.

The URI overlap across separate servers is not a problem: a resource URI is
unique only within its server, and every read is addressed to one server, so two
servers both exposing `theodosia://graph` never collide. Only a single server holding
two graphs needs distinct URIs, which is what `mount_multi` provides.
