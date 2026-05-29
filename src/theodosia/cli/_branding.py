"""Rose Pine theme + per-CLI branding singleton shared across cli modules."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.theme import Theme

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


def _set_branding(branding: _Branding) -> None:
    """Mutate the singleton in place so every module's ``from … import _BRANDING``
    reference stays valid. Rebinding instead of mutating would leave consumers
    (each ``cli`` submodule imports ``_BRANDING`` once at import time) holding
    the stale instance, which is the multi-module analogue of the bug that
    ``global _BRANDING; _BRANDING = …`` papered over when the CLI was one file.
    """
    for f in fields(_BRANDING):
        setattr(_BRANDING, f.name, getattr(branding, f.name))


# Burr UI deep links: /project/<project>/<partition_key>/<app_id>.
# The UI route reads the literal string "null" as the no-partition sentinel.
_BURR_UI_DEFAULT_HOST = "localhost"
_BURR_UI_DEFAULT_PORT = 7241
