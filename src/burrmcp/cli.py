"""``burrmcp`` CLI: launch or validate an importable Burr Application.

Built with Typer, so ``burrmcp --help`` and ``burrmcp <subcommand>
--help`` render structured help with the option types, defaults, and
short descriptions baked in.

Usage:

    burrmcp serve coffee_order:build_application --mode step
    burrmcp serve mymodule:application_factory --mode dynamic --name coffee
    burrmcp doctor coffee_order:build_application

The ``module:attr`` syntax matches uvicorn / gunicorn conventions. The
referenced attribute is either a built ``burr.core.Application``
(shared across sessions) or a callable factory returning one (one
build per session for state isolation). See ``burrmcp.mount`` for
the distinction. The ``doctor`` subcommand runs static validation
against the resolved Application before you mount it.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from burrmcp.adapter import ServingMode, mount

app = typer.Typer(
    name="burrmcp",
    help="Mount a Burr Application as an MCP server.",
    no_args_is_help=True,
    add_completion=False,
)


def _import_target(target: str, extra_paths: list[str] | None = None) -> Any:
    """Resolve ``module:attr`` into a Python object.

    Prepends the current working directory and any ``extra_paths`` to
    ``sys.path`` so users can point at their own modules from a checkout
    (the convention uvicorn / gunicorn follow). Raises ``SystemExit``
    with a clear message on import or attribute failure so the CLI
    doesn't dump a stack trace at the user.
    """
    if ":" not in target:
        raise SystemExit(
            f"target must be of the form module:attr (got {target!r}). "
            f"Example: coffee_order:build_application"
        )
    paths = [os.getcwd(), *(extra_paths or [])]
    for p in paths:
        absp = os.path.abspath(p)
        if absp not in sys.path:
            sys.path.insert(0, absp)
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import module {module_name!r}: {exc}") from exc
    if not hasattr(module, attr):
        available = ", ".join(sorted(n for n in dir(module) if not n.startswith("_")))
        raise SystemExit(
            f"module {module_name!r} has no attribute {attr!r}. "
            f"Available top-level names: {available}"
        )
    return getattr(module, attr)


@app.command()
def serve(
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "Import target in module:attr form. The attr is either a "
                "burr.core.Application or a callable returning one."
            ),
        ),
    ],
    mode: Annotated[
        ServingMode,
        typer.Option(
            "--mode",
            help="Serving mode.",
            case_sensitive=False,
        ),
    ] = ServingMode.STEP,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="MCP server name surfaced to clients (default: derived from target).",
        ),
    ] = None,
    app_dir: Annotated[
        list[str] | None,
        typer.Option(
            "--app-dir",
            help=(
                "Extra directory to prepend to sys.path before importing the target. "
                "Repeatable. Use this when your FSM module lives in a subdirectory "
                "of the project (e.g. --app-dir ./examples)."
            ),
        ),
    ] = None,
) -> None:
    """Launch an importable Burr Application or factory as an MCP server."""
    application_or_factory = _import_target(target, app_dir or [])
    server_name = name or target.split(":", 1)[0].split(".")[-1]
    server = mount(
        application_or_factory,
        mode=mode,
        name=server_name,
    )
    server.run()


@app.command()
def doctor(
    target: Annotated[
        str,
        typer.Argument(help="Import target in module:attr form. Same shape as `serve`."),
    ],
    app_dir: Annotated[
        list[str] | None,
        typer.Option(
            "--app-dir",
            help="Extra directory to prepend to sys.path before importing the target.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print message and details for every check, not just failures and warnings.",
        ),
    ] = False,
) -> None:
    """Statically validate a Burr Application or factory before mounting."""
    from burrmcp.doctor import format_report, run_checks

    application_or_factory = _import_target(target, app_dir or [])
    report = run_checks(application_or_factory)
    typer.echo(format_report(report, verbose=verbose))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def watch(
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help=(
                "Tracker project name. Defaults to the most-recently-touched "
                "project under --burr-home."
            ),
        ),
    ] = None,
    app_id: Annotated[
        str | None,
        typer.Option(
            "--app-id",
            help=(
                "App id (Burr's session uuid). Defaults to the most-recently-touched "
                "app in the chosen project."
            ),
        ),
    ] = None,
    list_projects: Annotated[
        bool,
        typer.Option(
            "--list",
            help="List available projects and recent apps, then exit.",
        ),
    ] = False,
    burr_home: Annotated[
        Path | None,
        typer.Option(
            "--burr-home",
            help="Override the tracker storage root. Defaults to ~/.burr.",
        ),
    ] = None,
    poll_interval: Annotated[
        float,
        typer.Option(
            "--poll",
            help="Polling interval in seconds when tailing.",
        ),
    ] = 0.5,
) -> None:
    """Tail and pretty-print a Burr tracker JSONL for a running session.

    With no flags, watches the most-recently-touched app under the most-
    recently-touched project. Use `burr://session` from a connected MCP
    client (or `--list`) to find the exact coordinates when running
    multiple servers at once. Stop with Ctrl-C.
    """
    home = (burr_home or Path.home() / ".burr").expanduser()
    if not home.exists():
        typer.echo(
            f"No Burr tracker storage at {home}. Has any tracked Burr Application run?",
            err=True,
        )
        raise typer.Exit(code=1)

    if list_projects:
        _print_listing(home)
        raise typer.Exit(code=0)

    resolved_project, resolved_app = _resolve_target(home, project, app_id)
    if resolved_project is None or resolved_app is None:
        typer.echo(f"Found no tracked sessions under {home}.", err=True)
        raise typer.Exit(code=1)

    log_path = home / resolved_project / resolved_app / "log.jsonl"
    if not log_path.exists():
        typer.echo(f"No log file at {log_path}.", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"watch  {resolved_project}/{resolved_app}")
    typer.echo(f"file   {log_path}")
    typer.echo("---")
    _tail_render(log_path, poll_interval=poll_interval)


def _resolve_target(
    home: Path, project: str | None, app_id: str | None
) -> tuple[str | None, str | None]:
    """Fill in missing project/app_id with most-recently-touched defaults.

    A directory whose name starts with '.' is treated as Burr metadata
    and skipped; this keeps a future `.burr/projects-meta` style folder
    from showing up as a fake project.
    """
    if project is None:
        candidates = [p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if not candidates:
            return None, None
        project = max(candidates, key=lambda p: p.stat().st_mtime).name

    proj_path = home / project
    if not proj_path.is_dir():
        typer.echo(f"No such project directory: {proj_path}", err=True)
        raise typer.Exit(code=1)

    if app_id is None:
        candidates = [p for p in proj_path.iterdir() if p.is_dir()]
        if not candidates:
            return project, None
        app_id = max(candidates, key=lambda p: p.stat().st_mtime).name

    return project, app_id


def _print_listing(home: Path, *, apps_per_project: int = 5) -> None:
    project_dirs = sorted(
        (p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not project_dirs:
        typer.echo(f"No tracked sessions under {home}.")
        return
    for proj in project_dirs:
        typer.echo(f"{proj.name}/")
        app_dirs = sorted(
            (p for p in proj.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for app in app_dirs[:apps_per_project]:
            log = app / "log.jsonl"
            mtime = datetime.fromtimestamp(app.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            size = log.stat().st_size if log.exists() else 0
            typer.echo(f"  {app.name}  ({mtime}, {size} bytes)")
        extra = len(app_dirs) - apps_per_project
        if extra > 0:
            typer.echo(f"  ... {extra} more")


def _tail_render(path: Path, *, poll_interval: float = 0.5) -> None:
    """Tail a Burr tracker JSONL and pretty-print each `end_entry`.

    First pass renders any content already on disk so the user sees the
    session history up to now; then polls for appends. File truncation
    (e.g., rotation) restarts from the top.
    """
    last_size = 0
    with path.open() as f:
        for line in f:
            _render_line(line)
        last_size = f.tell()

    try:
        while True:
            time.sleep(poll_interval)
            current_size = path.stat().st_size
            if current_size > last_size:
                with path.open() as f:
                    f.seek(last_size)
                    for line in f:
                        _render_line(line)
                    last_size = f.tell()
            elif current_size < last_size:
                last_size = 0
    except KeyboardInterrupt:
        typer.echo("\n(stopped)")


def _render_line(line: str) -> None:
    """Render one JSONL record. Only `end_entry` rows produce output."""
    line = line.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    if record.get("type") != "end_entry":
        return
    ts_short = _short_timestamp(record.get("end_time", ""))
    seq = record.get("sequence_id", "?")
    action = record.get("action", "?")
    exception = record.get("exception")
    if exception:
        typer.echo(f"[{ts_short}] seq {seq:>3}  {action:<20}  ERROR: {str(exception)[:60]}")
        return
    state = record.get("state") or {}
    state_view = {k: v for k, v in state.items() if not k.startswith("__")}
    items = list(state_view.items())[:6]
    state_repr = ", ".join(f"{k}={_short_value(v)}" for k, v in items)
    if len(state_view) > 6:
        state_repr += ", ..."
    typer.echo(f"[{ts_short}] seq {seq:>3}  {action:<20}  {state_repr}")


def _short_timestamp(ts: str) -> str:
    if "T" in ts:
        return ts.split("T", 1)[1].split(".", 1)[0]
    return ts


def _short_value(value: Any, *, limit: int = 30) -> str:
    s = repr(value) if isinstance(value, str) else str(value)
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``argv`` is for testing; ``None`` lets Typer read
    ``sys.argv`` normally."""
    try:
        rv = app(args=argv, standalone_mode=False)
        # With ``standalone_mode=False`` Typer/Click returns the exit
        # code from any in-callback ``typer.Exit(code=N)`` as the call's
        # return value rather than raising. Pass it through.
        return rv if isinstance(rv, int) else 0
    except typer.Exit as e:
        return e.exit_code or 0
    except SystemExit as e:
        # ``_import_target`` raises ``SystemExit(message)`` with a string
        # code on import/attribute failure. Surface the message to stderr
        # and return a clean nonzero so callers see a structured error
        # rather than a stack trace.
        if e.code is None:
            return 0
        if isinstance(e.code, int):
            return e.code
        print(e.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
