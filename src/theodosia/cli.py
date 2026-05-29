"""``theodosia`` CLI: serve, validate, and observe Burr Applications mounted as MCP servers.

Subcommands:

  theodosia serve <target>         Mount an importable Burr Application or factory.
  theodosia doctor <target>        Statically validate (and optionally probe at runtime).
  theodosia ui                     Launch Burr's web UI.
  theodosia sessions ls            Table of recent tracked sessions.
  theodosia sessions show <id>     Full post-mortem timeline of one session.
  theodosia sessions tail [id]     Live-tail a running session (rich render).
  theodosia watch [id]             Alias for `sessions tail`.
  theodosia logs [id]              Compact one-line-per-step log, greppable.

Every observability command reads ``~/.theodosia`` (Burr's
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
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from theodosia.adapter import ServingMode, mount
from theodosia.primer import primer

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

    prog_name: str = "theodosia"
    application: Any | None = None  # Application, factory, or "module:attr"
    server_name: str | None = None
    ui_extra: str = "theodosia[ui]"
    home: str | Path | None = None  # default tracker storage_dir
    upstream: dict[str, Any] | None = None  # other MCP servers actions can call


_BRANDING = _Branding()


# == target import (shared by serve + doctor) =========================


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
    """Tracker storage root: explicit flag, then the build_cli default, then ~/.theodosia.

    Theodosia keeps its LLM-driven session traces in ~/.theodosia by default, so
    they stay separate from code-driven Burr runs in ~/.burr. If ~/.theodosia has
    no sessions yet but ~/.burr does, fall back to ~/.burr and print a hint, so a
    user whose tracker still writes to Burr's default does not see an empty store.
    """
    if home is not None:
        return Path(home).expanduser()
    if _BRANDING.home is not None:
        return Path(_BRANDING.home).expanduser()
    theodosia_home = Path("~/.theodosia").expanduser()
    burr_home = Path("~/.burr").expanduser()
    if not theodosia_home.exists() and burr_home.exists() and any(burr_home.iterdir()):
        err_console.print(
            "[muted]no sessions in ~/.theodosia; reading ~/.burr "
            "(pass [bold]--home[/] to choose explicitly)[/]"
        )
        return burr_home
    return theodosia_home


# == render: the mounted graph as text ================================


@dataclass
class _Topology:
    name: str
    entry: str | None
    actions: list[str]
    edges: list[tuple[str, str, str | None]]  # (from, to, condition)

    def out_edges(self, node: str) -> list[tuple[str, str | None]]:
        return [(to, cond) for frm, to, cond in self.edges if frm == node]

    def is_terminal(self, node: str) -> bool:
        return not any(frm == node for frm, _to, _c in self.edges)

    def has_self_loop(self, node: str) -> bool:
        return any(frm == node == to for frm, to, _c in self.edges)


def _topology(target: str | None, app_dir: list[str], name: str) -> _Topology:
    app_or_factory, derived = _resolve_serve_target(target, app_dir)
    app = app_or_factory() if callable(app_or_factory) else app_or_factory
    graph = app.graph
    entry = graph.entrypoint.name if getattr(graph, "entrypoint", None) else None
    actions = [a.name for a in graph.actions]
    edges = [(t.from_.name, t.to.name, _condition_label(t.condition)) for t in graph.transitions]
    return _Topology(name=name or derived, entry=entry, actions=actions, edges=edges)


def _condition_label(condition: Any) -> str | None:
    """Human label for a transition condition, or None for the default (always)."""
    name = getattr(condition, "name", None)
    return None if not name or name == "default" else name


def _render_mermaid(topo: _Topology, *, conditions: bool) -> str:
    lines = ["stateDiagram-v2"]
    if topo.entry:
        lines.append(f"    [*] --> {topo.entry}")
    for frm, to, cond in topo.edges:
        label = f" : {cond}" if conditions and cond else ""
        lines.append(f"    {frm} --> {to}{label}")
    lines.extend(f"    {node} --> [*]" for node in topo.actions if topo.is_terminal(node))
    return "\n".join(lines)


def _render_dot(topo: _Topology, *, conditions: bool) -> str:
    lines = ["digraph G {", "    rankdir=LR;", "    node [shape=box, style=rounded];"]
    if topo.entry:
        lines.extend(("    __start__ [shape=point];", f'    __start__ -> "{topo.entry}";'))
    for frm, to, cond in topo.edges:
        label = f' [label="{cond}"]' if conditions and cond else ""
        lines.append(f'    "{frm}" -> "{to}"{label};')
    lines.append("}")
    return "\n".join(lines)


def _session_progress(
    home: Path, project: str | None, app_id: str | None
) -> tuple[str, str | None, set[str]]:
    """Resolve a tracked session and return (app_id, current_action, visited)."""
    log_path, _proj, aid = _resolve_app(home, project, app_id)
    rows = _read_steps(log_path)
    advanced = [r for r in rows if r.status != "error"]
    visited = {r.action for r in advanced}
    current = advanced[-1].action if advanced else None
    return aid, current, visited


def _graph_renderable(
    topo: _Topology,
    *,
    conditions: bool,
    current: str | None = None,
    visited: frozenset[str] | set[str] = frozenset(),
    session: str | None = None,
) -> Group:
    """Build the terminal graph view, optionally annotated with a session.

    When ``current``/``visited`` are given, the current node is highlighted and
    nodes the session hasn't reached are dimmed.
    """
    annotated = current is not None or bool(visited)
    header = Text()
    header.append(topo.name, style="header")
    header.append(f"  ·  {len(topo.actions)} action(s)", style="muted")
    if topo.entry:
        header.append("  ·  entry: ", style="muted")
        header.append(topo.entry, style="action")
    if session:
        header.append("  ·  session: ", style="muted")
        header.append(session, style="subtle")
    if current:
        header.append("  ·  at: ", style="muted")
        header.append(current, style="running")
    lines: list[Text] = [header, Text("─" * 52, style="muted")]
    width = max((len(a) for a in topo.actions), default=0)
    ordered = ([topo.entry] if topo.entry in topo.actions else []) + [
        a for a in topo.actions if a != topo.entry
    ]
    for node in ordered:
        terminal = topo.is_terminal(node)
        if node == current:
            marker, mstyle = "●", "running"
        elif node == topo.entry:
            marker, mstyle = "▶", "ok"
        elif terminal:
            marker, mstyle = "■", "err"
        else:
            marker, mstyle = " ", "muted"
        if node == current:
            name_style = "running"
        elif annotated and node not in visited:
            name_style = "muted"
        else:
            name_style = "action"
        line = Text()
        line.append(f" {marker} ", style=mstyle)
        line.append(f"{node:<{width}}", style=name_style)
        line.append(" ↺" if topo.has_self_loop(node) else "  ", style="running")
        if terminal:
            line.append("  (terminal)", style="muted")
        else:
            line.append("  → ", style="subtle")
            for i, (to, cond) in enumerate(topo.out_edges(node)):
                if i:
                    line.append(" · ", style="muted")
                line.append(to, style="subtle")
                if conditions and cond:
                    line.append(f" [{cond}]", style="muted")
        lines.append(line)
    return Group(*lines)


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
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="Transport: stdio (default), http, sse, or streamable-http.",
            case_sensitive=False,
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address for http/sse transports."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="Port for http/sse transports."),
    ] = 8000,
) -> None:
    """Launch an importable Burr Application or factory as an MCP server."""
    application_or_factory, derived_name = _resolve_serve_target(target, app_dir or [])
    server = mount(
        application_or_factory,
        mode=mode,
        name=name or derived_name,
        upstream=_BRANDING.upstream,
    )
    transport_norm = transport.lower()
    if transport_norm == "stdio":
        server.run(transport="stdio")
    elif transport_norm == "http":
        server.run(transport="http", host=host, port=port)
    elif transport_norm == "sse":
        server.run(transport="sse", host=host, port=port)
    elif transport_norm == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        raise typer.BadParameter(
            f"unknown transport {transport!r}; expected stdio, http, sse, or streamable-http"
        )


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
    from theodosia.doctor import format_report, run_checks

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


def render(
    target: Annotated[
        str | None,
        typer.Argument(help="Import target in module:attr form. Same shape as `serve`."),
    ] = None,
    app_dir: Annotated[
        list[str] | None,
        typer.Option("--app-dir", help="Extra sys.path directory before importing."),
    ] = None,
    mermaid: Annotated[
        bool, typer.Option("--mermaid", help="Emit Mermaid stateDiagram source instead.")
    ] = False,
    dot: Annotated[bool, typer.Option("--dot", help="Emit Graphviz DOT source instead.")] = False,
    conditions: Annotated[
        bool, typer.Option("--conditions", help="Show transition conditions on edges.")
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch", help="Live-render, highlighting the current node as a session runs."
        ),
    ] = False,
    app_id: Annotated[
        str | None,
        typer.Option("--app-id", help="Tracked session to annotate (defaults to most recent)."),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project of the session to annotate.")
    ] = None,
    home: Annotated[
        Path | None, typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia.")
    ] = None,
) -> None:
    """Render the mounted state machine as a diagram.

    Default is a static terminal view. ``--mermaid`` / ``--dot`` emit diagram
    source for docs. Pass ``--watch`` (or ``--app-id`` / ``--project``) to
    annotate the graph with a tracked session: the current node is highlighted
    and nodes the run hasn't reached are dimmed. Reads the graph statically, no
    server or Graphviz needed.
    """
    server_name = _BRANDING.server_name or _BRANDING.prog_name
    topo = _topology(target, app_dir or [], server_name)
    if mermaid and dot:
        raise SystemExit("choose one of --mermaid / --dot, not both")
    if mermaid:
        print(_render_mermaid(topo, conditions=conditions))
        return
    if dot:
        print(_render_dot(topo, conditions=conditions))
        return

    annotate = watch or app_id is not None or project is not None
    if not annotate:
        console.print(_graph_renderable(topo, conditions=conditions))
        return

    home = _resolve_home(home)

    def frame() -> Group:
        aid, current, visited = _session_progress(home, project, app_id)
        return _graph_renderable(
            topo, conditions=conditions, current=current, visited=visited, session=aid
        )

    if not watch:
        console.print(frame())
        return
    try:
        with Live(frame(), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(0.5)
                live.update(frame())
    except KeyboardInterrupt:
        console.print("[dim](stopped)[/]")


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
    state_raw: dict[str, Any] | None = None  # full state dict including __PRIOR_STEP


def _read_refusals(log_path: Path) -> list[StepRow]:
    """Read the refusals.jsonl sidecar (written by the adapter) next to the
    tracker log. These are blocked transitions the agent attempted; Burr's own
    log never sees them because the action never ran."""
    sidecar = log_path.parent / "refusals.jsonl"
    if not sidecar.exists():
        return []
    rows: list[StepRow] = []
    with sidecar.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            reason = rec.get("refusal_reason") or "refused"
            msg = rec.get("error_message")
            rows.append(
                StepRow(
                    seq=rec.get("seq", -1),
                    action=rec.get("action", "?"),
                    started=rec.get("ts", ""),
                    duration_ms=None,
                    status="error",
                    error_summary=f"{reason}: {msg}" if msg else reason,
                    state_summary={},
                )
            )
    return rows


def _terminal_state_may_be_stale(steps: list[StepRow]) -> bool:
    """Heuristic for the Burr astep+sync-action staleness on the terminal row.

    Burr records pre-step state in ``end_entry`` when the action body is sync.
    For non-terminal rows the CLI scans forward; the terminal row has no
    forward entry, so its state stays stale if the body was sync. Detect by
    checking whether ``__PRIOR_STEP`` in the row's snapshot names the row's
    action (true post-state) or the previous action (stale pre-state).
    """
    if not steps:
        return False
    last = steps[-1]
    raw = getattr(last, "state_raw", None)
    if raw is None:
        return False
    return raw.get("__PRIOR_STEP") != last.action


def _read_steps(log_path: Path) -> list[StepRow]:
    """Pair begin/end entries from a Burr tracker JSONL into rows.

    Works around Burr's sync-action staleness: when an ``@action`` body is
    sync, ``post_run_step`` (the source of ``end_entry``) fires with
    pre-step state. We detect this per-row by checking ``__PRIOR_STEP`` in
    the recorded state. If it does not match the row's action, we scan
    forward for the entry whose ``__PRIOR_STEP`` does match. That entry's
    state is the true post-step state for this row.
    """
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

    def _post_state(seq: int, action_name: str) -> dict[str, Any]:
        e = ends.get(seq)
        if e is None:
            return {}
        candidate = e.get("state") or {}
        if candidate.get("__PRIOR_STEP") == action_name:
            return candidate
        # Scan forward for the entry whose __PRIOR_STEP names this action.
        for later_seq in sorted(s for s in ends if s > seq):
            later_state = ends[later_seq].get("state") or {}
            if later_state.get("__PRIOR_STEP") == action_name:
                return later_state
        # Last action with no forward entry; the recorded state is the best we have.
        return candidate

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
        action_name = b.get("action", "?")
        state = _post_state(seq, action_name)
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
                    state_raw=state,
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
                    state_raw=state,
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


def _relative_when(ts: str) -> str:
    """Render an ISO timestamp as a short relative-time label (3m, 2h, 4d)."""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    delta = datetime.now() - when
    s = int(delta.total_seconds())
    if s < 0:
        return ts.split("T", 1)[-1].split(".", 1)[0]
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _status_text(status: str) -> Text:
    if status == "ok":
        return Text("✓", style="ok")
    if status == "error":
        return Text("✗", style="err")
    if status == "empty":
        return Text("∅", style="muted")
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
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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
    home = _resolve_home(home)
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
            # An empty tracker dir means the session was created but never
            # took a step. Calling that "running" is wrong; it's idle.
            last_status = rows[-1].status if rows else "empty"
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
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a rich table.")
    ] = False,
) -> None:
    """Full post-mortem timeline of one session."""
    home = _resolve_home(home)
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
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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


# == CLI assembly =====================================================


def _version_callback(value: bool) -> None:
    if not value:
        return
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("theodosia")
    except PackageNotFoundError:
        v = "unknown"
    console.print(f"theodosia {v}")
    raise typer.Exit()


def report(
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
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Write the report to a file. Default: print to stdout.",
        ),
    ] = None,
    webhook: Annotated[
        str | None,
        typer.Option(
            "--webhook",
            help=(
                "POST the report as application/markdown to this URL. "
                "Useful for Slack/Discord/Linear/PagerDuty hooks. The post "
                "happens in addition to --out / stdout, not instead."
            ),
        ),
    ] = None,
) -> None:
    """Generate a markdown post-mortem for a tracked session.

    The report covers session metadata, an ordered timeline of every action
    (with status and any error summary), refusals (blocked transitions the
    agent attempted), and the final state of the FSM. Compatible with any
    Burr session theodosia has tracked; reads from ``~/.theodosia`` (or
    ``--home``) without requiring the original mounted server to be
    running. Optionally POSTs the rendered markdown to a webhook for
    Slack, Discord, Linear, PagerDuty, or any inbox that accepts
    ``application/markdown``.
    """
    resolved_home = _resolve_home(home)
    log_path, proj, aid = _resolve_app(resolved_home, project, app_id)
    steps = _read_steps(log_path)
    refusals = _read_refusals(log_path)
    markdown = _render_session_report(
        project=proj, app_id=aid, log_path=log_path, steps=steps, refusals=refusals
    )

    if out is not None:
        out.expanduser().write_text(markdown, encoding="utf-8")
        err_console.print(
            Text.assemble(
                ("wrote report: ", "muted"),
                (str(out.expanduser()), "ok"),
            )
        )
    else:
        print(markdown)

    if webhook is not None:
        _post_report(webhook, markdown, project=proj, app_id=aid)


def _render_session_report(
    *,
    project: str,
    app_id: str,
    log_path: Path,
    steps: list[StepRow],
    refusals: list[StepRow],
) -> str:
    """Render a markdown post-mortem from a session's step rows and refusals."""
    lines: list[str] = [
        f"# Session report: `{project}` / `{app_id}`",
        "",
        f"- Log path: `{log_path}`",
        f"- Steps recorded: {len(steps)}",
        f"- Refusals recorded: {len(refusals)}",
    ]
    if steps:
        first = steps[0]
        last = steps[-1]
        total_ms = sum(s.duration_ms or 0 for s in steps)
        lines.extend(
            (
                f"- First action: `{first.action}` at {first.started}",
                f"- Last action: `{last.action}` ({last.status}) at {last.started}",
                f"- Total action duration: {total_ms:.1f} ms",
            )
        )
    lines.append("")

    if steps:
        lines.extend(
            (
                "## Timeline",
                "",
                "| seq | action | status | started | dur (ms) | error |",
                "|---:|---|---|---|---:|---|",
            )
        )
        for s in steps:
            dur = f"{s.duration_ms:.1f}" if s.duration_ms is not None else "-"
            err = (s.error_summary or "").replace("|", "\\|")[:80]
            lines.append(f"| {s.seq} | `{s.action}` | {s.status} | {s.started} | {dur} | {err} |")
        lines.append("")

    if refusals:
        lines.extend(
            (
                "## Refusals",
                "",
                "Refusals are transitions the agent attempted that the FSM rejected. "
                "They never advanced state; they are recorded here for postmortem.",
                "",
                "| seq | action | ts | reason |",
                "|---:|---|---|---|",
            )
        )
        for r in refusals:
            reason = (r.error_summary or "").replace("|", "\\|")[:80]
            lines.append(f"| {r.seq} | `{r.action}` | {r.started} | {reason} |")
        lines.append("")

    if steps:
        final_state = steps[-1].state_summary
        if final_state:
            lines.append("## Final state")
            lines.append("")
            if _terminal_state_may_be_stale(steps):
                lines.append(
                    "> Note: the terminal action's post-state cannot be read back "
                    "from Burr's tracker when the action body is sync (Burr fires "
                    "`post_run_step` with pre-action state in that case). The "
                    "snapshot below is one step behind for a sync terminal; for "
                    "the true post-action state read `theodosia://state` from a "
                    "live session or write the action body `async def`."
                )
                lines.append("")
            lines.append("```json")
            lines.append(json.dumps(final_state, indent=2, default=str))
            lines.append("```")
            lines.append("")

    if not steps and not refusals:
        lines.extend(("_(no steps or refusals recorded at this session yet)_", ""))

    return "\n".join(lines)


def _post_report(webhook_url: str, markdown: str, *, project: str, app_id: str) -> None:
    """POST the rendered report to a webhook as ``application/markdown``."""
    import urllib.request

    req = urllib.request.Request(
        webhook_url,
        data=markdown.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/markdown; charset=utf-8",
            "X-Theodosia-Project": project,
            "X-Theodosia-App-Id": app_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            err_console.print(
                Text.assemble(
                    ("posted to ", "muted"),
                    (webhook_url, "ok"),
                    (f" (HTTP {resp.status})", "muted"),
                )
            )
    except Exception as exc:
        err_console.print(
            Text.assemble(
                ("webhook POST failed: ", "err"),
                (f"{type(exc).__name__}: {exc}", "err"),
            )
        )
        raise typer.Exit(code=2) from exc


def status(
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        _theodosia_version = _pkg_version("theodosia")
    except PackageNotFoundError:
        _theodosia_version = "0+unknown"

    resolved_home = _resolve_home(home)
    payload: dict[str, Any] = {
        "theodosia_version": _theodosia_version,
        "storage_home": str(resolved_home),
        "storage_exists": resolved_home.exists(),
        "projects": [],
    }
    if resolved_home.exists():
        project_dirs = sorted(
            (p for p in resolved_home.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for proj in project_dirs:
            app_dirs = [p for p in proj.iterdir() if p.is_dir()]
            recent = sorted(app_dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            latest = recent[0] if recent else None
            latest_info: dict[str, Any] = {}
            if latest is not None:
                log = latest / "log.jsonl"
                rows = _read_steps(log) if log.exists() and log.stat().st_size > 0 else []
                latest_info = {
                    "app_id": latest.name,
                    "mtime": datetime.fromtimestamp(latest.stat().st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "steps": len(rows),
                    "last_action": rows[-1].action if rows else "(empty)",
                    "last_status": rows[-1].status if rows else "empty",
                }
            payload["projects"].append(
                {
                    "name": proj.name,
                    "sessions": len(app_dirs),
                    "latest": latest_info,
                }
            )

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    console.print(
        Text.assemble(
            ("● ", "header"),
            (f"theodosia {payload['theodosia_version']}\n", "header"),
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
        return

    table = Table(show_header=True, header_style="header", box=None, expand=False)
    table.add_column("project", style="action", no_wrap=True)
    table.add_column("sessions", justify="right", style="accent", no_wrap=True)
    table.add_column("app_id", style="muted", no_wrap=True)
    table.add_column("steps", justify="right", no_wrap=True)
    table.add_column("last action", style="action", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("when", style="subtle", no_wrap=True)
    for proj in payload["projects"]:
        latest_raw: Any = proj.get("latest") or {}
        latest_entry: dict[str, Any] = latest_raw if isinstance(latest_raw, dict) else {}
        status_text = latest_entry.get("last_status", "")
        status_style = {
            "ok": "ok",
            "running": "running",
            "failed": "err",
            "empty": "muted",
        }.get(status_text, "muted")
        table.add_row(
            proj["name"],
            str(proj["sessions"]),
            (latest_entry.get("app_id") or "")[:12],
            str(latest_entry.get("steps", 0)),
            (latest_entry.get("last_action") or "")[:18],
            Text(status_text or "(none)", style=status_style),
            _relative_when(latest_entry.get("mtime") or ""),
        )
    console.print()
    console.print(table)


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
        typer.Option("--home", help="Tracker storage root. Defaults to ~/.theodosia."),
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


def build_cli(
    prog_name: str = "theodosia",
    *,
    application: Any | None = None,
    help: str | None = None,
    server_name: str | None = None,
    ui_extra: str = "theodosia[ui]",
    home: str | Path | None = None,
    upstream: dict[str, Any] | None = None,
) -> typer.Typer:
    """Build a theodosia CLI, optionally rebranded for a downstream package.

    A package that ships its own MCP graph can expose its own command::

        # my_fsm_mcp/cli.py
        from theodosia.cli import build_cli, run
        from my_fsm_mcp import build_application

        cli = build_cli("my-fsm-mcp", application=build_application,
                        help="My graph as an MCP server.")

        def main() -> int:
            return run(cli)

    Then ``my-fsm-mcp serve`` (no target needed), ``my-fsm-mcp doctor``, and
    ``my-fsm-mcp sessions ls`` all carry the downstream's name. Sessions are
    still stored in Burr's tracker format; set ``home`` to match the
    ``storage_dir`` the downstream's ``LocalTrackingClient`` writes to.

    Args:
        prog_name: command name shown in help and used as the default
            server name when a baked-in Application has no other name.
        application: an ``Application``, a factory, or a ``module:attr``
            string. When set, ``serve``/``doctor`` accept no target.
        help: root help text. Defaults to the theodosia description.
        server_name: default MCP server name surfaced to clients.
        ui_extra: pip extra named in the ``ui`` install hint.
        home: default tracker storage root for the observability
            commands. Overridden per-invocation by ``--home``.
        upstream: map of server name to a ``fastmcp.Client`` transport.
            Action bodies reach these other MCP servers with
            ``call_upstream(server, tool, args)``. Passed through to
            ``mount`` by ``serve``.
    """
    global _BRANDING
    _BRANDING = _Branding(
        prog_name=prog_name,
        application=application,
        server_name=server_name,
        ui_extra=ui_extra,
        home=home,
        upstream=upstream,
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
    cli.command()(render)
    cli.command()(ui)
    cli.command()(watch)
    cli.command()(logs)
    cli.command()(verify)
    cli.command()(status)
    cli.command()(report)
    cli.command()(primer)

    @cli.callback()
    def _root(
        version: bool = typer.Option(
            False,
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ) -> None:
        pass

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
    """Default ``theodosia`` entry point."""
    return run(app, argv)


if __name__ == "__main__":
    sys.exit(main())
