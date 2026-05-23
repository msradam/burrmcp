"""burrmcp: mount Burr Applications as MCP servers.

State lives on the server. Every mounted server exposes a constant
four-tool surface (``step``, ``reset_session``, ``fork_at``,
``fork_from_past``) regardless of FSM complexity; the action namespace
lives in ``step``'s argument schema and at ``burr://graph``. Plus two
synthetic tools from FastMCP's ``ResourcesAsTools`` transform
(``list_resources``, ``read_resource``) for clients that don't
implement native ``resources/read`` (IBM Bob Shell as of mid-2026).

``TOOLS`` and ``DYNAMIC`` serving modes were carved into
``burrmcp._experimental.modes`` once ``STEP`` became the sole product;
the ``ServingMode`` enum keeps ``STEP`` as its only member so callers
passing ``mode=ServingMode.STEP`` keep working.
"""

from burrmcp.adapter import (
    ServingMode,
    ValidationFailed,
    current_mcp_context,
    mount,
    mount_multi,
    spawn_subapp,
)
from burrmcp.importing import ToolSpec, burr_app_from_fastmcp

__all__ = [
    "ServingMode",
    "ToolSpec",
    "ValidationFailed",
    "burr_app_from_fastmcp",
    "current_mcp_context",
    "mount",
    "mount_multi",
    "spawn_subapp",
]
