"""theodosia: mount Burr Applications as MCP servers.

State lives on the server. Every mounted server exposes a constant
four-tool surface (``step``, ``reset_session``, ``fork_at``,
``fork_from_past``) regardless of FSM complexity; the action namespace
lives in ``step``'s argument schema and at ``theodosia://graph``. Plus two
synthetic tools from FastMCP's ``ResourcesAsTools`` transform
(``list_resources``, ``read_resource``) for clients that don't
implement native ``resources/read`` (IBM Bob Shell as of mid-2026).

``TOOLS`` and ``DYNAMIC`` serving modes were carved into
``theodosia._experimental.modes`` once ``STEP`` became the sole product;
the ``ServingMode`` enum keeps ``STEP`` as its only member so callers
passing ``mode=ServingMode.STEP`` keep working.
"""

from typing import Any

from theodosia.adapter import (
    ServingMode,
    ValidationFailed,
    current_mcp_context,
    mount,
    mount_multi,
    spawn_subapp,
)
from theodosia.assembly import Assembly
from theodosia.importing import ToolSpec, burr_app_from_fastmcp
from theodosia.ledger import HashChainedLedger, verify_ledger
from theodosia.upstream import (
    ERROR,
    MALFORMED,
    OK,
    SourceResult,
    UpstreamError,
    UpstreamManager,
    bind_upstream,
    call_upstream,
    classify_payload,
    confidence_label,
    coverage,
    safe_upstream,
)


def tracker(project: str, storage_dir: str = "~/.theodosia", **kwargs: Any):
    """A Burr ``LocalTrackingClient`` that defaults its store to ``~/.theodosia``.

    Use this in your builder (``.with_tracker(theodosia.tracker("my-project"))``)
    to keep LLM-driven session traces separate from code-driven Burr runs, which
    use Burr's own ``~/.burr`` default. It is a thin wrapper; pass any
    ``LocalTrackingClient`` keyword through, including a different ``storage_dir``.
    """
    from burr.tracking.client import LocalTrackingClient

    return LocalTrackingClient(project=project, storage_dir=storage_dir, **kwargs)


__all__ = [
    "ERROR",
    "MALFORMED",
    "OK",
    "Assembly",
    "HashChainedLedger",
    "ServingMode",
    "SourceResult",
    "ToolSpec",
    "UpstreamError",
    "UpstreamManager",
    "ValidationFailed",
    "bind_upstream",
    "burr_app_from_fastmcp",
    "call_upstream",
    "classify_payload",
    "confidence_label",
    "coverage",
    "current_mcp_context",
    "mount",
    "mount_multi",
    "safe_upstream",
    "spawn_subapp",
    "tracker",
    "verify_ledger",
]
