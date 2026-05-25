# CLI

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
