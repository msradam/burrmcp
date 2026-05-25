"""``burrmcp`` CLI: serve, validate, and observe Burr Applications mounted as MCP servers.

Subcommands:

  burrmcp serve <target>         Mount an importable Burr Application or factory.
  burrmcp doctor <target>        Statically validate (and optionally probe at runtime).
  burrmcp ui                     Launch Burr's web UI.
  burrmcp sessions ls            Table of recent tracked sessions.
  burrmcp sessions show <id>     Full post-mortem timeline of one session.
  burrmcp sessions tail [id]     Live-tail a running session (rich render).
  burrmcp watch [id]             Alias for `sessions tail`.
  burrmcp logs [id]              Compact one-line-per-step log, greppable.

Every observability command reads ``~/.burr`` (Burr's
``LocalTrackingClient`` storage), so it works against any session a
mounted server has written, including those running right now in
another process.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from burrmcp.adapter import ServingMode, mount

# Rose Pine palette (https://rosepinetheme.com). Semantic style names map
# onto the palette so the rendering code reads intent, not hex.
_ROSE_PINE = {
    "love": "#eb6f92",  # red    -> errors / refusals
    "gold": "#f6c177",  # yellow -> running / pending
    "rose": "#ebbcba",  # accent
    "pine": "#31748f",  # teal
    "foam": "#9ccfd8",  # cyan   -> success
    "iris": "#c4a7e7",  # purple -> headers / actions
    "muted": "#6e6a86",  # dim
    "subtle": "#908caa",  # secondary text
    "text": "#e0def4",
}
_THEME = Theme(
    {
        "ok": f"bold {_ROSE_PINE['foam']}",
        "err": f"bold {_ROSE_PINE['love']}",
        "running": f"bold {_ROSE_PINE['gold']}",
        "action": f"bold {_ROSE_PINE['iris']}",
        "accent": _ROSE_PINE["rose"],
        "muted": _ROSE_PINE["muted"],
        "subtle": _ROSE_PINE["subtle"],
        "header": f"bold {_ROSE_PINE['iris']}",
        "link": _ROSE_PINE["foam"],
        "repr.str": _ROSE_PINE["text"],
    }
)

console = Console(theme=_THEME)
err_console = Console(stderr=True, theme=_THEME)

_DEFAULT_HELP = "Mount a Burr Application as an MCP server, with rich terminal observability."


@dataclass
class _Branding:
    """Per-CLI configuration set by ``build_cli``.

    A downstream package that ships its own command (``my-fsm-mcp serve``)
    stamps its name, bakes in its graph so ``serve``/``doctor`` need no
    target, and points the observability commands at its tracker store.
    A console script is its own process and builds exactly one CLI, so a
    module-level singleton is the right scope.
    """

    prog_name: str = "burrmcp"
    application: Any | None = None  # Application, factory, or "module:attr"
    server_name: str | None = None
    ui_extra: str = "burrmcp[ui]"
    burr_home: str | Path | None = None  # default tracker storage_dir


_BRANDING = _Branding()


# == target import (shared by serve + doctor) =========================


def _import_target(target: str, extra_paths: list[str] | None = None) -> Any:
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


def _resolve_serve_target(target: str | None, app_dir: list[str]) -> tuple[Any, str]:
    """Resolve the Application (or factory) to serve, plus a default name.

    ``target`` wins when given; otherwise fall back to a graph baked in via
    ``build_cli(application=...)``. The baked-in value may be an object, a
    factory, or a ``module:attr`` string.
    """
    src = _BRANDING.application if target is None else target
    if src is None:
        raise SystemExit(
            "serve needs a target in module:attr form (e.g. coffee_order:build_application), "
            "or a graph baked in via build_cli(application=...)."
        )
    if isinstance(src, str):
        return _import_target(src, app_dir), src.split(":", 1)[0].split(".")[-1]
    return src, _BRANDING.server_name or _BRANDING.prog_name


def _resolve_home(burr_home: Path | None) -> Path:
    """Tracker storage root: explicit flag, then the build_cli default, then ~/.burr."""
    chosen = burr_home or _BRANDING.burr_home or (Path.home() / ".burr")
    return Path(chosen).expanduser()


# == serve / doctor / ui ==============================================


def serve(
    target: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Import target in module:attr form. The attr is either a "
                "burr.core.Application or a callable returning one. Optional "
                "when a graph is baked in via build_cli(application=...)."
            ),
        ),
    ] = None,
    mode: Annotated[
        ServingMode,
        typer.Option("--mode", help="Serving mode.", case_sensitive=False),
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
                "Extra directory to prepend to sys.path before importing. "
                "Repeatable. Use when the FSM module is in a subdirectory."
            ),
        ),
    ] = None,
) -> None:
    """Launch an importable Burr Application or factory as an MCP server."""
    application_or_factory, derived_name = _resolve_serve_target(target, app_dir or [])
    server = mount(application_or_factory, mode=mode, name=name or derived_name)
    server.run()


def doctor(
    target: Annotated[
        str | None,
        typer.Argument(help="Import target in module:attr form. Same shape as `serve`."),
    ] = None,
    app_dir: Annotated[
        list[str] | None,
        typer.Option("--app-dir", help="Extra sys.path directory before importing."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print every check, not just failures and warnings."),
    ] = False,
    runtime: Annotated[
        bool,
        typer.Option(
            "--runtime",
            help=(
                "Also mount the server in-process and probe its wire shape: "
                "tool listing, resource catalog, step result content blocks."
            ),
        ),
    ] = False,
) -> None:
    """Statically validate a Burr Application or factory before mounting."""
    from burrmcp.doctor import format_report, run_checks

    application_or_factory, _ = _resolve_serve_target(target, app_dir or [])
    report = run_checks(application_or_factory, runtime=runtime)
    typer.echo(format_report(report, verbose=verbose))
    if not report.ok:
        raise typer.Exit(code=1)


def ui(
    port: Annotated[int, typer.Option("--port", help="Port for the Burr UI server.")] = 7241,
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address. Use 0.0.0.0 to expose on the network."),
    ] = "127.0.0.1",
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open a browser tab when the UI starts.")
    ] = False,
) -> None:
    """Launch the Burr UI to inspect tracked sessions.

    Prefers the local install if apache-burr\\[start] is present (one
    process). Otherwise shells out to ``uvx --from 'apache-burr\\[start]'``.
    """
    import shutil
    import subprocess

    forwarded = ["--port", str(port), "--host", host]
    if no_open:
        forwarded.append("--no-open")

    try:
        import loguru  # noqa: F401 (probe-only)

        cmd = [
            sys.executable,
            "-c",
            "from burr.cli.__main__ import cli_run_server; cli_run_server()",
            *forwarded,
        ]
    except ImportError:
        if shutil.which("uvx") is None:
            err_console.print(
                "the Burr UI needs either apache-burr[start] installed in the "
                f"current env (try [bold]uv pip install '{_BRANDING.ui_extra}'[/]) or "
                "[bold]uvx[/] on PATH (https://docs.astral.sh/uv/) for one-shot bootstrap."
            )
            raise typer.Exit(code=1) from None
        cmd = ["uvx", "--from", "apache-burr[start]", "burr", *forwarded]

    console.print(f"Launching Burr UI on [link]http://{host}:{port}[/link]")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(code=exc.returncode or 1) from exc
    except KeyboardInterrupt:
        pass


# == sessions: tracker-store inspection ==============================


@dataclass
class StepRow:
    seq: int
    action: str
    started: str
    duration_ms: float | None
    status: str  # "ok" | "error" | "running"
    error_summary: str | None
    state_summary: dict[str, Any]


def _read_steps(log_path: Path) -> list[StepRow]:
    """Pair begin/end entries from a Burr tracker JSONL into rows."""
    begins: dict[int, dict] = {}
    ends: dict[int, dict] = {}
    if not log_path.exists():
        return []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = rec.get("sequence_id")
            if seq is None:
                continue
            if rec.get("type") == "begin_entry":
                begins[seq] = rec
            elif rec.get("type") == "end_entry":
                ends[seq] = rec
    rows: list[StepRow] = []
    for seq in sorted(begins):
        b = begins[seq]
        e = ends.get(seq)
        started = b.get("start_time", "")
        if e is None:
            rows.append(
                StepRow(
                    seq=seq,
                    action=b.get("action", "?"),
                    started=started,
                    duration_ms=None,
                    status="running",
                    error_summary=None,
                    state_summary={},
                )
            )
            continue
        duration_ms = _duration_ms(started, e.get("end_time", ""))
        exc = e.get("exception")
        state = e.get("state") or {}
        state_view = {k: v for k, v in state.items() if not k.startswith("__")}
        if exc:
            err_first_line = _exception_summary(str(exc))
            rows.append(
                StepRow(
                    seq=seq,
                    action=b.get("action", "?"),
                    started=started,
                    duration_ms=duration_ms,
                    status="error",
                    error_summary=err_first_line[:140],
                    state_summary=state_view,
                )
            )
        else:
            rows.append(
                StepRow(
                    seq=seq,
                    action=b.get("action", "?"),
                    started=started,
                    duration_ms=duration_ms,
                    status="ok",
                    error_summary=None,
                    state_summary=state_view,
                )
            )
    return rows


def _exception_summary(exc: str) -> str:
    """Pull the human-meaningful message out of a stored exception.

    Tracker exceptions are full tracebacks; the bare last line is often a
    stray `)` from a multi-line call. Prefer the last line that looks like
    `SomeError: message`, else the last non-empty line.
    """
    import re

    lines = [ln.rstrip() for ln in exc.strip().splitlines() if ln.strip()]
    if not lines:
        return "exception"
    for ln in reversed(lines):
        if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Failed|Warning):", ln.strip()):
            return ln.strip()[:160]
    return lines[-1].strip()[:160]


def _duration_ms(start: str, end: str) -> float | None:
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return None
    return round((e - s).total_seconds() * 1000, 1)


def _short_value(value: Any, *, limit: int = 28) -> str:
    if isinstance(value, str):
        s = value
    elif isinstance(value, (list, tuple)):
        s = f"[{len(value)} items]"
    elif isinstance(value, dict):
        s = f"{{{len(value)} keys}}"
    else:
        s = str(value)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _state_diff_text(
    state: dict[str, Any],
    prev_state: dict[str, Any] | None,
    *,
    max_items: int = 4,
) -> str:
    """Show only what changed since the previous step.

    For step 0 (no prev), show non-empty fields. For subsequent steps,
    show keys whose value differs from prev. This is what makes the
    timeline scan-able: each row says "this step changed X, Y".
    """
    if prev_state is None:
        changed = {k: v for k, v in state.items() if v not in (None, "", [], {}, False)}
    else:
        changed = {k: v for k, v in state.items() if prev_state.get(k) != v}
    if not changed:
        return "(no state change)"
    items = list(changed.items())[:max_items]
    parts = [f"{k}={_short_value(v)}" for k, v in items]
    if len(changed) > max_items:
        parts.append(f"+{len(changed) - max_items}")
    return ", ".join(parts)


def _short_ts(ts: str) -> str:
    if "T" in ts:
        return ts.split("T", 1)[1].split(".", 1)[0]
    return ts


def _status_text(status: str) -> Text:
    if status == "ok":
        return Text("✓", style="ok")
    if status == "error":
        return Text("✗", style="err")
    return Text("•", style="running")


def _build_steps_table(
    rows: list[StepRow], *, project: str, app_id: str, title_suffix: str = ""
) -> Table:
    title = f"[header]{project}[/] / [muted]{app_id}[/]"
    if title_suffix:
        title += f"  {title_suffix}"
    table = Table(
        title=title,
        title_justify="left",
        expand=True,
        show_lines=False,
        border_style="muted",
    )
    table.add_column("seq", justify="right", width=4, no_wrap=True, style="muted")
    table.add_column("time", width=8, no_wrap=True, style="subtle")
    table.add_column("", width=1, no_wrap=True)  # status glyph
    table.add_column("action", style="action", no_wrap=True)
    table.add_column("ms", justify="right", width=7, no_wrap=True, style="muted")
    table.add_column("state / error")
    prev_state: dict[str, Any] | None = None
    for r in rows:
        if r.status == "error":
            state_cell = Text(r.error_summary or "error", style="err")
        elif r.status == "running":
            state_cell = Text("(running...)", style="running")
        else:
            state_cell = Text(_state_diff_text(r.state_summary, prev_state), style="subtle")
        ms = "" if r.duration_ms is None else f"{r.duration_ms:.0f}"
        table.add_row(
            str(r.seq),
            _short_ts(r.started),
            _status_text(r.status),
            r.action,
            ms,
            state_cell,
        )
        if r.status != "error":
            prev_state = r.state_summary
    return table


# == sessions ls ======================================================


def sessions_ls(
    burr_home: Annotated[
        Path | None,
        typer.Option("--burr-home", help="Tracker storage root. Defaults to ~/.burr."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Filter to a single project."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Max recent apps to show per project."),
    ] = 8,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a rich table.")
    ] = False,
) -> None:
    """Table of recent tracked sessions, most recent first."""
    home = _resolve_home(burr_home)
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

    payload: list[dict] = []
    for proj in project_dirs:
        app_dirs = sorted(
            (p for p in proj.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        entries = []
        for a in app_dirs:
            log = a / "log.jsonl"
            size = log.stat().st_size if log.exists() else 0
            rows = _read_steps(log) if size > 0 else []
            last_action = rows[-1].action if rows else "(empty)"
            last_status = rows[-1].status if rows else "running"
            entries.append(
                {
                    "app_id": a.name,
                    "mtime": datetime.fromtimestamp(a.stat().st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "size_bytes": size,
                    "steps": len(rows),
                    "last_action": last_action,
                    "last_status": last_status,
                }
            )
        payload.append({"project": proj.name, "apps": entries})

    if as_json:
        console.print_json(json.dumps(payload))
        return

    if not payload:
        console.print(f"[dim]No projects under {home}[/]")
        return

    for proj_entry in payload:
        table = Table(
            title=f"[header]{proj_entry['project']}/[/]",
            title_justify="left",
            expand=True,
            show_lines=False,
            border_style="muted",
        )
        table.add_column("app_id", no_wrap=True, style="muted")
        table.add_column("last touched", no_wrap=True, style="subtle")
        table.add_column("steps", justify="right", width=6, no_wrap=True)
        table.add_column("", width=1, no_wrap=True)
        table.add_column("last action", no_wrap=True, style="action")
        table.add_column("bytes", justify="right", style="muted")
        for app_entry in proj_entry["apps"]:
            table.add_row(
                app_entry["app_id"],
                app_entry["mtime"].replace("T", " "),
                str(app_entry["steps"]),
                _status_text(app_entry["last_status"]),
                app_entry["last_action"],
                str(app_entry["size_bytes"]),
            )
        console.print(table)
        console.print()


# == sessions show ====================================================


def _resolve_app(home: Path, project: str | None, app_id: str | None) -> tuple[Path, str, str]:
    """Resolve project + app_id (each optional) into a concrete log directory.

    Both default to the most-recently-touched. Returns (log_path, project, app_id).
    """
    if project is None:
        candidates = [p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if not candidates:
            err_console.print(f"[err]No tracked projects under[/] {home}")
            raise typer.Exit(code=1)
        project = max(candidates, key=lambda p: p.stat().st_mtime).name

    proj_path = home / project
    if not proj_path.is_dir():
        err_console.print(f"[err]No such project directory:[/] {proj_path}")
        raise typer.Exit(code=1)

    if app_id is None:
        app_candidates = [p for p in proj_path.iterdir() if p.is_dir()]
        if not app_candidates:
            err_console.print(f"[err]No apps under project[/] {project}")
            raise typer.Exit(code=1)
        app_id = max(app_candidates, key=lambda p: p.stat().st_mtime).name

    if not (proj_path / app_id).is_dir():
        # Prefix match: `sessions show abc123` matches a uuid starting with it.
        matches = [p.name for p in proj_path.iterdir() if p.is_dir() and p.name.startswith(app_id)]
        if len(matches) == 1:
            app_id = matches[0]
        else:
            err_console.print(
                f"[err]No app[/] {app_id!r} [err]in project[/] {project!r}"
                + (f" (ambiguous prefix matches: {matches})" if len(matches) > 1 else "")
            )
            raise typer.Exit(code=1)

    return proj_path / app_id / "log.jsonl", project, app_id


def sessions_show(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    burr_home: Annotated[
        Path | None,
        typer.Option("--burr-home", help="Tracker storage root. Defaults to ~/.burr."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a rich table.")
    ] = False,
) -> None:
    """Full post-mortem timeline of one session."""
    home = _resolve_home(burr_home)
    log_path, proj, aid = _resolve_app(home, project, app_id)
    rows = _read_steps(log_path)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "project": proj,
                    "app_id": aid,
                    "log_path": str(log_path),
                    "steps": [r.__dict__ for r in rows],
                }
            )
        )
        return

    if not rows:
        console.print(f"[dim]No steps recorded yet at {log_path}[/]")
        return

    table = _build_steps_table(
        rows,
        project=proj,
        app_id=aid,
        title_suffix=f"  {len(rows)} step(s)",
    )
    console.print(table)


# == sessions tail / watch ===========================================


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
    burr_home: Annotated[
        Path | None,
        typer.Option("--burr-home", help="Tracker storage root. Defaults to ~/.burr."),
    ] = None,
    poll_interval: Annotated[
        float, typer.Option("--poll", help="Polling interval in seconds.")
    ] = 0.5,
) -> None:
    """Live-tail a running (or completed) session as a rich-rendered table."""
    home = _resolve_home(burr_home)
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
    burr_home: Annotated[
        Path | None,
        typer.Option("--burr-home", help="Tracker storage root. Defaults to ~/.burr."),
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
    home = _resolve_home(burr_home)
    if list_projects:
        sessions_ls(burr_home=home, project=None, limit=8, as_json=False)
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
    burr_home: Annotated[
        Path | None,
        typer.Option("--burr-home", help="Tracker storage root. Defaults to ~/.burr."),
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
    state change. Pipe it: `burrmcp logs --plain | grep error`.
    """
    home = _resolve_home(burr_home)
    log_path, _proj, _aid = _resolve_app(home, project, app_id)
    rows = _read_steps(log_path)
    if refusals_only:
        rows = [r for r in rows if r.status == "error"]
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


# == CLI assembly =====================================================


def build_cli(
    prog_name: str = "burrmcp",
    *,
    application: Any | None = None,
    help: str | None = None,
    server_name: str | None = None,
    ui_extra: str = "burrmcp[ui]",
    burr_home: str | Path | None = None,
) -> typer.Typer:
    """Build a burrmcp CLI, optionally rebranded for a downstream package.

    A package that ships its own MCP graph can expose its own command::

        # my_fsm_mcp/cli.py
        from burrmcp.cli import build_cli, run
        from my_fsm_mcp import build_application

        cli = build_cli("my-fsm-mcp", application=build_application,
                        help="My graph as an MCP server.")

        def main() -> int:
            return run(cli)

    Then ``my-fsm-mcp serve`` (no target needed), ``my-fsm-mcp doctor``, and
    ``my-fsm-mcp sessions ls`` all carry the downstream's name. Sessions are
    still stored in Burr's tracker format; set ``burr_home`` to match the
    ``storage_dir`` the downstream's ``LocalTrackingClient`` writes to.

    Args:
        prog_name: command name shown in help and used as the default
            server name when a baked-in Application has no other name.
        application: an ``Application``, a factory, or a ``module:attr``
            string. When set, ``serve``/``doctor`` accept no target.
        help: root help text. Defaults to the burrmcp description.
        server_name: default MCP server name surfaced to clients.
        ui_extra: pip extra named in the ``ui`` install hint.
        burr_home: default tracker storage root for the observability
            commands. Overridden per-invocation by ``--burr-home``.
    """
    global _BRANDING
    _BRANDING = _Branding(
        prog_name=prog_name,
        application=application,
        server_name=server_name,
        ui_extra=ui_extra,
        burr_home=burr_home,
    )

    cli = typer.Typer(
        name=prog_name,
        help=help or _DEFAULT_HELP,
        no_args_is_help=True,
        add_completion=False,
    )
    sessions = typer.Typer(
        name="sessions",
        help="Inspect Burr tracker storage: list, show, or live-tail a session.",
        no_args_is_help=True,
    )
    sessions.command("ls")(sessions_ls)
    sessions.command("show")(sessions_show)
    sessions.command("tail")(sessions_tail)
    cli.add_typer(sessions, name="sessions")

    cli.command()(serve)
    cli.command()(doctor)
    cli.command()(ui)
    cli.command()(watch)
    cli.command()(logs)
    return cli


app = build_cli()


def run(cli: typer.Typer, argv: list[str] | None = None) -> int:
    """Run a Typer app with graceful exit-code handling. ``argv`` is for tests."""
    try:
        rv = cli(args=argv, standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except typer.Exit as e:
        return e.exit_code or 0
    except SystemExit as e:
        if e.code is None:
            return 0
        if isinstance(e.code, int):
            return e.code
        err_console.print(str(e.code))
        return 1


def main(argv: list[str] | None = None) -> int:
    """Default ``burrmcp`` entry point."""
    return run(app, argv)


if __name__ == "__main__":
    sys.exit(main())
