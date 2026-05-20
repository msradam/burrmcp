"""burr-mcp: mount Burr Applications as MCP servers.

Each Burr @action becomes an MCP tool. State lives on the server.
Transitions can be enforced server-side.

Three serving modes:

  • tools:   every @action exposed as its own MCP tool, no gating.
             Closest to a flat MCP server today.
  • step:    one ``step(action_name, **args)`` meta-tool. Server
             enforces valid transitions. Works on every MCP client,
             including those that ignore tools/list_changed.
  • dynamic: per-action tools whose visibility tracks current state.
             Best when the client honors dynamic tool lists.

Default is ``step`` because client support for dynamic tool lists is
patchy as of mid-2026: Claude Code ignores list_changed, Cursor
doesn't refresh on its own.
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
