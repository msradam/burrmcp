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
  theodosia status                 Tracker storage + recent activity snapshot.
  theodosia report [id]            Markdown post-mortem of one session.
  theodosia verify [id]            Verify a session's tamper-evident ledger.
  theodosia render <target>        Render the FSM graph as text / mermaid / dot.

Every observability command reads ``~/.theodosia`` (Burr's
``LocalTrackingClient`` storage), so it works against any session a
mounted server has written, including those running right now in
another process.

Implementation lives in submodules grouped by command family. This
``__init__`` keeps the public import surface flat: existing callers
``from theodosia.cli import build_cli, run, app`` and tests reaching for
``_burr_ui_url`` / ``_read_steps`` / etc keep working unchanged.
"""

from __future__ import annotations

import sys

from theodosia.cli._app import build_cli, doctor, run, serve, ui
from theodosia.cli._branding import _BRANDING, _Branding, console, err_console
from theodosia.cli._resolve import (
    _bail,
    _burr_ui_url,
    _import_target,
    _locate_project_home,
    _pick_default_app_id,
    _pick_default_project,
    _resolve_app,
    _resolve_app_id_prefix,
    _resolve_home,
    _resolve_serve_target,
)
from theodosia.cli._steps import (
    StepRow,
    _build_steps_table,
    _duration_ms,
    _exception_summary,
    _read_refusals,
    _read_steps,
    _relative_when,
    _scan_app_entry,
    _short_ts,
    _short_value,
    _state_diff_text,
    _status_text,
    _terminal_state_may_be_stale,
)
from theodosia.cli._topology import (
    _condition_label,
    _graph_renderable,
    _render_dot,
    _render_mermaid,
    _session_progress,
    _Topology,
    _topology,
    render,
)
from theodosia.cli.reports import _post_report, _render_session_report, report
from theodosia.cli.sessions import (
    logs,
    sessions_ls,
    sessions_show,
    sessions_tail,
    watch,
)
from theodosia.cli.status import (
    _collect_status_payload,
    _scan_project_latest,
    _theodosia_installed_version,
    status,
    verify,
)

__all__ = [
    "_BRANDING",
    "StepRow",
    "_Branding",
    "_Topology",
    "_bail",
    "_build_steps_table",
    "_burr_ui_url",
    "_collect_status_payload",
    "_condition_label",
    "_duration_ms",
    "_exception_summary",
    "_graph_renderable",
    "_import_target",
    "_locate_project_home",
    "_pick_default_app_id",
    "_pick_default_project",
    "_post_report",
    "_read_refusals",
    "_read_steps",
    "_relative_when",
    "_render_dot",
    "_render_mermaid",
    "_render_session_report",
    "_resolve_app",
    "_resolve_app_id_prefix",
    "_resolve_home",
    "_resolve_serve_target",
    "_scan_app_entry",
    "_scan_project_latest",
    "_session_progress",
    "_short_ts",
    "_short_value",
    "_state_diff_text",
    "_status_text",
    "_terminal_state_may_be_stale",
    "_theodosia_installed_version",
    "_topology",
    "app",
    "build_cli",
    "console",
    "doctor",
    "err_console",
    "logs",
    "main",
    "render",
    "report",
    "run",
    "serve",
    "sessions_ls",
    "sessions_show",
    "sessions_tail",
    "status",
    "ui",
    "verify",
    "watch",
]


# Build the default Theodosia CLI at import time so downstream callers
# (``python -m theodosia``, ``theodosia.cli:app``) get a ready instance.
app = build_cli()


def main(argv: list[str] | None = None) -> int:
    """Default ``theodosia`` entry point."""
    return run(app, argv)


if __name__ == "__main__":
    sys.exit(main())
