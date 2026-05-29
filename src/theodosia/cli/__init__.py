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

# Intentional re-exports of underscored helpers that tests and
# ``theodosia.__init__`` reach for. The ``as X as X`` form tells ruff
# this isn't a stray import; keeping the names importable from
# ``theodosia.cli`` is part of the contract with existing call sites.
from theodosia.cli._branding import _BRANDING as _BRANDING
from theodosia.cli._branding import _Branding as _Branding

# Public command callables and the public StepRow model.
from theodosia.cli._branding import console, err_console
from theodosia.cli._resolve import _burr_ui_url as _burr_ui_url
from theodosia.cli._resolve import _import_target as _import_target
from theodosia.cli._resolve import _resolve_app as _resolve_app
from theodosia.cli._resolve import _resolve_home as _resolve_home
from theodosia.cli._resolve import _resolve_serve_target as _resolve_serve_target
from theodosia.cli._steps import StepRow
from theodosia.cli._steps import _exception_summary as _exception_summary
from theodosia.cli._steps import _read_refusals as _read_refusals
from theodosia.cli._steps import _read_steps as _read_steps
from theodosia.cli._steps import _state_diff_text as _state_diff_text
from theodosia.cli._topology import _condition_label as _condition_label
from theodosia.cli._topology import _graph_renderable as _graph_renderable
from theodosia.cli._topology import _session_progress as _session_progress
from theodosia.cli._topology import _Topology as _Topology

# Function name collides with submodule path ``theodosia.cli._topology``;
# this re-export shadows the submodule attribute so ``cli._topology(target,
# app_dir, name)`` calls the function as it did before the split.
from theodosia.cli._topology import _topology as _topology
from theodosia.cli._topology import render
from theodosia.cli.reports import report
from theodosia.cli.sessions import _diff_state_dicts as _diff_state_dicts
from theodosia.cli.sessions import (
    logs,
    sessions_diff,
    sessions_ls,
    sessions_show,
    sessions_tail,
    watch,
)
from theodosia.cli.status import status, verify

# Public surface only. Underscored helpers (``_read_steps``, ``_BRANDING``,
# ``_burr_ui_url`` etc.) are imported at the top of this module and remain
# importable from ``theodosia.cli`` for tests and downstream tooling, but
# they are not declared API and may move without notice.
__all__ = [
    "StepRow",
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
    "sessions_diff",
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
