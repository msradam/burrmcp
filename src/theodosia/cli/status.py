"""``theodosia status`` and ``theodosia verify``: tracker health + ledger check."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table
from rich.text import Text

from theodosia.cli._branding import (
    _BRANDING,
    _BURR_UI_DEFAULT_HOST,
    _BURR_UI_DEFAULT_PORT,
    console,
    err_console,
)
from theodosia.cli._resolve import _resolve_app, _resolve_home
from theodosia.cli._steps import _read_steps, _relative_when


def _theodosia_installed_version() -> str:
    """Resolve the version of the *running* CLI's package, falling back to
    Theodosia. Honors ``build_cli(prog_name=...)`` so a rebranded
    ``my-fsm status`` reports ``my-fsm <version>``.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    for pkg in (_BRANDING.prog_name, "theodosia"):
        try:
            return _pkg_version(pkg)
        except PackageNotFoundError:
            continue
    return "0+unknown"


def _scan_project_latest(proj_dir: Path) -> tuple[int, dict[str, Any]]:
    """Return ``(session_count, latest_info_dict)`` for one project dir."""
    app_dirs = [p for p in proj_dir.iterdir() if p.is_dir()]
    recent = sorted(app_dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    if not recent:
        return len(app_dirs), {}
    latest = recent[0]
    log = latest / "log.jsonl"
    rows = _read_steps(log) if log.exists() and log.stat().st_size > 0 else []
    return len(app_dirs), {
        "app_id": latest.name,
        "mtime": datetime.fromtimestamp(latest.stat().st_mtime).isoformat(timespec="seconds"),
        "steps": len(rows),
        "last_action": rows[-1].action if rows else "(empty)",
        "last_status": rows[-1].status if rows else "empty",
    }


def _collect_status_payload(resolved_home: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prog_name": _BRANDING.prog_name,
        "theodosia_version": _theodosia_installed_version(),
        "storage_home": str(resolved_home),
        "storage_exists": resolved_home.exists(),
        "projects": [],
    }
    if not resolved_home.exists():
        return payload
    project_dirs = sorted(
        (p for p in resolved_home.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for proj in project_dirs:
        session_count, latest_info = _scan_project_latest(proj)
        payload["projects"].append(
            {"name": proj.name, "sessions": session_count, "latest": latest_info}
        )
    return payload


def _print_status_header(payload: dict[str, Any]) -> None:
    console.print(
        Text.assemble(
            ("● ", "header"),
            (f"{payload['prog_name']} {payload['theodosia_version']}\n", "header"),
        )
    )
    storage_style = "ok" if payload["storage_exists"] else "err"
    console.print(
        Text.assemble(
            ("storage: ", "muted"),
            (str(payload["storage_home"]), storage_style),
            (" (exists)" if payload["storage_exists"] else " (missing)", storage_style),
        )
    )


def _print_status_empty_hint(resolved_home: Path) -> None:
    console.print(Text(f"\nno projects found in {resolved_home}.", style="muted"))
    burr_home = Path("~/.burr").expanduser()
    if (
        resolved_home == Path("~/.theodosia").expanduser()
        and burr_home.exists()
        and any(burr_home.iterdir())
    ):
        console.print(
            "[muted]Sessions exist under [bold]~/.burr[/] (Burr's native default). "
            "Try [bold]theodosia status --home ~/.burr[/].[/]"
        )


def _build_status_table(projects: list[dict[str, Any]]) -> Table:
    table = Table(show_header=True, header_style="header", box=None, expand=False)
    table.add_column("project", style="action", no_wrap=True)
    table.add_column("sessions", justify="right", style="accent", no_wrap=True)
    table.add_column("app_id", style="muted", no_wrap=True)
    table.add_column("steps", justify="right", no_wrap=True)
    table.add_column("last action", style="action", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("when", style="subtle", no_wrap=True)
    style_for = {"ok": "ok", "running": "running", "failed": "err", "empty": "muted"}
    for proj in projects:
        raw: Any = proj.get("latest") or {}
        entry: dict[str, Any] = raw if isinstance(raw, dict) else {}
        status_text = entry.get("last_status", "")
        table.add_row(
            proj["name"],
            str(proj["sessions"]),
            (entry.get("app_id") or "")[:12],
            str(entry.get("steps", 0)),
            (entry.get("last_action") or "")[:18],
            Text(status_text or "(none)", style=style_for.get(status_text, "muted")),
            _relative_when(entry.get("mtime") or ""),
        )
    return table


def status(
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of a rich summary."),
    ] = False,
) -> None:
    """One-shot snapshot: tracker storage, recent activity, project health.

    Useful as a smoke check ("is my tracker working?") and as a launch banner
    on demo recordings. Reads ``~/.theodosia`` by default.
    """
    resolved_home = _resolve_home(home)
    payload = _collect_status_payload(resolved_home)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    ui_host = os.environ.get("BURR_UI_HOST", _BURR_UI_DEFAULT_HOST)
    ui_port = int(os.environ.get("BURR_UI_PORT", _BURR_UI_DEFAULT_PORT))
    ui_root = f"http://{ui_host}:{ui_port}/"
    payload["burr_ui_url"] = ui_root

    _print_status_header(payload)
    if not payload["storage_exists"]:
        console.print()
        console.print(
            Text(
                "Run a mounted FSM at least once, or create the directory, "
                "to see project activity.",
                style="muted",
            )
        )
        return
    if not payload["projects"]:
        _print_status_empty_hint(resolved_home)
        return
    console.print()
    console.print(_build_status_table(payload["projects"]))
    console.print(f"\n[muted]Burr UI:[/] [link={ui_root}]{ui_root}[/]")


def verify(
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
) -> None:
    """Verify a session's tamper-evident ledger. Exits nonzero if the chain is broken.

    Every step and refusal is hash-chained into ``ledger.jsonl`` next to the
    session's tracker log. This recomputes the chain and names the exact line
    where any after-the-fact edit, reorder, or deletion broke it.
    """
    from theodosia.ledger import verify_ledger

    home = _resolve_home(home)
    log_path, proj, aid = _resolve_app(home, project, app_id)
    ledger_path = log_path.parent / "ledger.jsonl"
    if not ledger_path.exists():
        err_console.print(
            f"[err]No ledger for[/] {proj}/{aid} [muted](no ledger.jsonl; "
            "the session ran without a local tracker)[/]"
        )
        raise typer.Exit(code=1)
    ok, problems = verify_ledger(ledger_path)
    if ok:
        console.print(f"[ok]✓ ledger intact[/]  {proj}/{aid}")
        return
    err_console.print(f"[err]✗ ledger TAMPERED[/]  {proj}/{aid}")
    for p in problems:
        err_console.print(f"  [err]{p}[/]")
    raise typer.Exit(code=1)
