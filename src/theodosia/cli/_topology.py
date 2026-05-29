"""Static graph topology + the ``render`` command."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Group
from rich.live import Live
from rich.text import Text

from theodosia.cli._branding import _BRANDING, console
from theodosia.cli._resolve import _resolve_app, _resolve_home, _resolve_serve_target
from theodosia.cli._steps import _read_steps


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


def _graph_header(topo: _Topology, *, current: str | None, session: str | None) -> Text:
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
    return header


def _graph_node_marker(
    node: str, topo: _Topology, current: str | None, terminal: bool
) -> tuple[str, str]:
    if node == current:
        return "●", "running"
    if node == topo.entry:
        return "▶", "ok"
    if terminal:
        return "■", "err"
    return " ", "muted"


def _graph_node_name_style(
    node: str, current: str | None, annotated: bool, visited: frozenset[str] | set[str]
) -> str:
    if node == current:
        return "running"
    if annotated and node not in visited:
        return "muted"
    return "action"


def _graph_node_edges(
    line: Text, topo: _Topology, node: str, *, conditions: bool, terminal: bool
) -> None:
    if terminal:
        line.append("  (terminal)", style="muted")
        return
    line.append("  → ", style="subtle")
    for i, (to, cond) in enumerate(topo.out_edges(node)):
        if i:
            line.append(" · ", style="muted")
        line.append(to, style="subtle")
        if conditions and cond:
            line.append(f" [{cond}]", style="muted")


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
    lines: list[Text] = [_graph_header(topo, current=current, session=session)]
    lines.append(Text("─" * 52, style="muted"))
    width = max((len(a) for a in topo.actions), default=0)
    ordered = ([topo.entry] if topo.entry in topo.actions else []) + [
        a for a in topo.actions if a != topo.entry
    ]
    for node in ordered:
        terminal = topo.is_terminal(node)
        marker, mstyle = _graph_node_marker(node, topo, current, terminal)
        name_style = _graph_node_name_style(node, current, annotated, visited)
        line = Text()
        line.append(f" {marker} ", style=mstyle)
        line.append(f"{node:<{width}}", style=name_style)
        line.append(" ↺" if topo.has_self_loop(node) else "  ", style="running")
        _graph_node_edges(line, topo, node, conditions=conditions, terminal=terminal)
        lines.append(line)
    return Group(*lines)


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
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
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
