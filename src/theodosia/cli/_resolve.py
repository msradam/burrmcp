"""Resolution helpers: import targets, tracker home, app/project lookup, UI URLs."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

import typer

from theodosia.cli._branding import (
    _BRANDING,
    _BURR_UI_DEFAULT_HOST,
    _BURR_UI_DEFAULT_PORT,
    err_console,
)


def _import_target(target: str, extra_paths: list[str] | None = None) -> Any:
    if ":" not in target:
        raise SystemExit(
            f"target must be of the form module:attr (got {target!r}). "
            f"Example: coffee_order:build_application"
        )
    paths = [str(Path.cwd()), *(extra_paths or [])]
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


def _resolve_home(home: Path | None) -> Path:
    """Tracker storage root, in resolution-priority order:

    1. Explicit ``--home`` flag (per invocation).
    2. ``THEODOSIA_HOME`` env var (per process / per deployment).
    3. ``build_cli(home=...)`` default (per rebranded CLI).
    4. ``~/.theodosia`` (the default).

    Theodosia keeps its LLM-driven session traces in ``~/.theodosia`` by
    default, so they stay separate from code-driven Burr runs in
    ``~/.burr``. If ``~/.theodosia`` has no sessions yet but ``~/.burr``
    does, fall back to ``~/.burr`` and print a hint, so a user whose
    tracker still writes to Burr's default does not see an empty store.
    The fallback is suppressed when ``THEODOSIA_HOME`` is set, because
    an operator who set it explicitly does not need the implicit hint
    on every command.
    """
    if home is not None:
        return Path(home).expanduser()
    env_home = os.environ.get("THEODOSIA_HOME")
    if env_home:
        return Path(env_home).expanduser()
    if _BRANDING.home is not None:
        return Path(_BRANDING.home).expanduser()
    theodosia_home = Path("~/.theodosia").expanduser()
    burr_home = Path("~/.burr").expanduser()
    if not theodosia_home.exists() and burr_home.exists() and any(burr_home.iterdir()):
        err_console.print(
            "[muted]no sessions in ~/.theodosia; reading ~/.burr "
            "(pass [bold]--home[/] or set [bold]THEODOSIA_HOME[/] to choose explicitly)[/]"
        )
        return burr_home
    return theodosia_home


def _burr_ui_url(
    project: str,
    app_id: str,
    partition_key: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> str:
    """Burr UI deep link for one tracked session.

    Honors ``BURR_UI_HOST`` / ``BURR_UI_PORT`` env overrides so users running
    the UI behind a tunnel or on a non-default port still get clickable links.
    """
    h = host or os.environ.get("BURR_UI_HOST", _BURR_UI_DEFAULT_HOST)
    p = port or int(os.environ.get("BURR_UI_PORT", _BURR_UI_DEFAULT_PORT))
    pk = partition_key or "null"
    return f"http://{h}:{p}/project/{project}/{pk}/{app_id}"


def _bail(msg: str) -> None:
    err_console.print(msg)
    raise typer.Exit(code=1)


def _pick_default_project(home: Path) -> str:
    if not home.is_dir():
        _bail(f"[err]No tracker storage at[/] {home}")
    candidates = [p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not candidates:
        _bail(f"[err]No tracked projects under[/] {home}")
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def _pick_default_app_id(proj_path: Path, project: str) -> str:
    app_candidates = [p for p in proj_path.iterdir() if p.is_dir()]
    if not app_candidates:
        _bail(f"[err]No apps under project[/] {project}")
    return max(app_candidates, key=lambda p: p.stat().st_mtime).name


def _resolve_app_id_prefix(proj_path: Path, project: str, app_id: str) -> str:
    """Map a uuid prefix to a single concrete app id, or bail."""
    matches = [p.name for p in proj_path.iterdir() if p.is_dir() and p.name.startswith(app_id)]
    if len(matches) == 1:
        return matches[0]
    suffix = f" (ambiguous prefix matches: {matches})" if len(matches) > 1 else ""
    _bail(f"[err]No app[/] {app_id!r} [err]in project[/] {project!r}{suffix}")
    return app_id  # unreachable; _bail raises


def _resolve_app(home: Path, project: str | None, app_id: str | None) -> tuple[Path, str, str]:
    """Resolve project + app_id (each optional) into a concrete log directory.

    Both default to the most-recently-touched. Returns ``(log_path, project, app_id)``.
    """
    if project is None:
        project = _pick_default_project(home)
    proj_path = home / project
    if not proj_path.is_dir():
        _bail(f"[err]No such project directory:[/] {proj_path}")
    if app_id is None:
        app_id = _pick_default_app_id(proj_path, project)
    elif not (proj_path / app_id).is_dir():
        app_id = _resolve_app_id_prefix(proj_path, project, app_id)
    return proj_path / app_id / "log.jsonl", project, app_id


def _locate_project_home(home: Path | None, project: str | None) -> Path:
    """Resolve the tracker root, auto-falling back when the project lives under ``~/.burr``.

    A user who wired Burr's ``LocalTrackingClient(project=...)`` directly writes
    to ``~/.burr/<project>``; the CLI defaults to ``~/.theodosia`` and would
    report no such project. When a project name is given and exists under
    ``~/.burr`` but not the resolved default, switch silently and print a
    one-line hint so the user knows to pass ``--home ~/.burr`` next time.
    """
    if home is not None:
        return _resolve_home(home)
    default = _resolve_home(None)
    if project is None:
        return default
    if (default / project).is_dir():
        return default
    burr_home = Path("~/.burr").expanduser()
    if burr_home != default and (burr_home / project).is_dir():
        err_console.print(
            f"[muted]project {project!r} found under ~/.burr; "
            f"reading from there. Pass [bold]--home ~/.burr[/] to make it explicit.[/]"
        )
        return burr_home
    return default
