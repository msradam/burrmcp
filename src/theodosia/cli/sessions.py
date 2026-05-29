"""``theodosia sessions ls/show/tail`` plus ``watch`` and ``logs``."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.live import Live
from rich.table import Table
from rich.text import Text

from theodosia.cli._branding import console, err_console
from theodosia.cli._resolve import (
    _burr_ui_url,
    _locate_project_home,
    _resolve_app,
    _resolve_home,
)
from theodosia.cli._steps import (
    _build_steps_table,
    _read_refusals,
    _read_steps,
    _relative_when,
    _scan_app_entry,
    _short_ts,
    _state_diff_text,
    _status_text,
)


def _collect_sessions_payload(
    project_dirs: list[Path], *, limit: int, show_all: bool
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for proj in project_dirs:
        app_dirs = sorted(
            (p for p in proj.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        entries = [
            entry for a in app_dirs if (entry := _scan_app_entry(a, show_all=show_all)) is not None
        ]
        payload.append({"project": proj.name, "apps": entries})
    return payload


def _build_sessions_table(proj_entry: dict[str, Any]) -> Table:
    table = Table(
        title=f"[header]{proj_entry['project']}/[/]",
        title_justify="left",
        expand=True,
        show_lines=False,
        border_style="muted",
    )
    table.add_column("app_id", no_wrap=True, style="muted", width=12)
    table.add_column("when", no_wrap=True, style="subtle", width=8)
    table.add_column("steps", justify="right", width=6, no_wrap=True)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("last action", no_wrap=True, style="action")
    for app_entry in proj_entry["apps"]:
        table.add_row(
            app_entry["app_id"][:12],
            _relative_when(app_entry["mtime"]),
            str(app_entry["steps"]),
            _status_text(app_entry["last_status"]),
            (app_entry["last_action"] or "")[:18],
        )
    return table


def sessions_ls(
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Filter to a single project."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Max recent apps to show per project."),
    ] = 8,
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help=(
                "Include empty tracker entries (created by FastMCP on connect "
                "but never advanced). Default hides them."
            ),
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a rich table.")
    ] = False,
) -> None:
    """Table of recent tracked sessions, most recent first."""
    home = _locate_project_home(home, project)
    if not home.exists():
        err_console.print(f"[err]No Burr tracker storage at[/] {home}")
        raise typer.Exit(code=1)

    project_dirs = sorted(
        (p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if project:
        project_dirs = [p for p in project_dirs if p.name == project]
        if not project_dirs:
            err_console.print(f"[err]No such project under[/] {home}: {project!r}")
            raise typer.Exit(code=1)

    payload = _collect_sessions_payload(project_dirs, limit=limit, show_all=show_all)

    if as_json:
        console.print_json(json.dumps(payload))
        return

    if not payload:
        console.print(f"[dim]No projects under {home}[/]")
        return

    for proj_entry in payload:
        console.print(_build_sessions_table(proj_entry))
        console.print()


def sessions_show(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a rich table.")
    ] = False,
    open_ui: Annotated[
        bool,
        typer.Option(
            "--open",
            help="Open this session in the Burr UI (http://localhost:7241) in the default browser.",
        ),
    ] = False,
) -> None:
    """Full post-mortem timeline of one session."""
    home = _resolve_home(home)
    log_path, proj, aid = _resolve_app(home, project, app_id)
    rows = _read_steps(log_path)
    ui_url = _burr_ui_url(proj, aid)

    if open_ui:
        import webbrowser

        webbrowser.open(ui_url)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "project": proj,
                    "app_id": aid,
                    "log_path": str(log_path),
                    "burr_ui_url": ui_url,
                    "steps": [r.__dict__ for r in rows],
                }
            )
        )
        return

    if not rows:
        console.print(f"[dim]No steps recorded yet at {log_path}[/]")
        console.print(f"[muted]Burr UI:[/] [link={ui_url}]{ui_url}[/]")
        return

    table = _build_steps_table(
        rows,
        project=proj,
        app_id=aid,
        title_suffix=f"  {len(rows)} step(s)",
    )
    console.print(table)
    console.print(f"[muted]Burr UI:[/] [link={ui_url}]{ui_url}[/]")


def _tail(log_path: Path, *, project: str, app_id: str, poll_interval: float) -> None:
    """Live-render the tracker log via rich.Live."""

    def render() -> Table:
        rows = _read_steps(log_path)
        suffix = f"  [dim]· {len(rows)} step(s) · polling {poll_interval}s · Ctrl-C to stop[/]"
        return _build_steps_table(rows, project=project, app_id=app_id, title_suffix=suffix)

    try:
        with Live(render(), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(poll_interval)
                live.update(render())
    except KeyboardInterrupt:
        console.print("[dim](stopped)[/]")


def sessions_tail(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    poll_interval: Annotated[
        float, typer.Option("--poll", help="Polling interval in seconds.")
    ] = 0.5,
) -> None:
    """Live-tail a running (or completed) session as a rich-rendered table."""
    home = _resolve_home(home)
    log_path, proj, aid = _resolve_app(home, project, app_id)
    _tail(log_path, project=proj, app_id=aid, poll_interval=poll_interval)


def watch(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    list_projects: Annotated[
        bool,
        typer.Option(
            "--list",
            help="(Deprecated alias for `sessions ls`.) List projects and exit.",
        ),
    ] = False,
    poll_interval: Annotated[
        float, typer.Option("--poll", help="Polling interval in seconds.")
    ] = 0.5,
) -> None:
    """Alias for `sessions tail`. Lives at the top level for muscle memory."""
    home = _resolve_home(home)
    if list_projects:
        sessions_ls(home=home, project=None, limit=8, as_json=False)
        return
    if not home.exists():
        err_console.print(f"[err]No Burr tracker storage at[/] {home}")
        raise typer.Exit(code=1)
    log_path, proj, aid = _resolve_app(home, project, app_id)
    _tail(log_path, project=proj, app_id=aid, poll_interval=poll_interval)


def logs(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    refusals_only: Annotated[
        bool,
        typer.Option("--refusals", help="Show only the steps that errored (refusals)."),
    ] = False,
    plain: Annotated[
        bool,
        typer.Option("--plain", help="No color, no glyphs; pipe-friendly for grep."),
    ] = False,
) -> None:
    """Compact one-line-per-step log of a session, greppable.

    The terse sibling of `sessions show` (rich table) and `sessions tail`
    (live). One line per step: seq, time, status, action, duration, and the
    state change. Pipe it: `theodosia logs --plain | grep error`.
    """
    home = _resolve_home(home)
    log_path, _proj, _aid = _resolve_app(home, project, app_id)
    rows = _read_steps(log_path)
    if refusals_only:
        rows = [r for r in rows if r.status == "error"] + _read_refusals(log_path)
        rows.sort(key=lambda r: (r.started, r.seq))
    if not rows:
        console.print("[muted](no steps)[/]" if not plain else "(no steps)")
        return
    prev: dict[str, Any] | None = None
    for r in rows:
        ms = "" if r.duration_ms is None else f"{r.duration_ms:.0f}ms"
        detail = (
            r.error_summary or "error"
            if r.status == "error"
            else _state_diff_text(r.state_summary, prev)
        )
        if r.status != "error":
            prev = r.state_summary
        if plain:
            mark = {"ok": "OK", "error": "ERR", "running": "...."}[r.status]
            console.print(
                f"{r.seq:>3}  {_short_ts(r.started)}  {mark:<4} {r.action:<22} {ms:>7}  {detail}",
                highlight=False,
                markup=False,
            )
        else:
            glyph = _status_text(r.status)
            line = Text.assemble(
                (f"{r.seq:>3} ", "muted"),
                (f"{_short_ts(r.started)} ", "subtle"),
                glyph,
                (f" {r.action:<22} ", "action"),
                (f"{ms:>7}  ", "muted"),
                (detail, "err" if r.status == "error" else "subtle"),
            )
            console.print(line)
