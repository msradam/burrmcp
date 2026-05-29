"""On-disk tracker helpers: locate the log file, read the trace, name the project.

Burr's ``LocalTrackingClient`` writes a per-session ``log.jsonl`` under
its ``storage_dir/<project>/<app_id>/``. These helpers wrap the
introspection a little so the rest of the adapter does not have to
reach into ``app._tracker`` directly.

Reaches into Burr's ``app._tracker`` slot. We pin Burr to a minor
version range in ``pyproject.toml`` for exactly this reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from burr.core import Application

# Cap ``theodosia://trace`` response to the last N records. Burr's
# tracker is append-only; long-running sessions accumulate; an MCP
# client doesn't want the full multi-MB log returned over the wire.
_TRACE_MAX_ENTRIES = 1000


def _tracker_project(app: Application) -> str | None:
    """Return the LocalTrackingClient project name, or None.

    Surfaced on every step/fork meta-tool response so even collapsed
    tool-result views in MCP clients carry enough to locate the
    session's data on disk (``~/.burr/<project>/<app_id>/``).
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    return tracker.project_id


def _tracker_log_path(app: Application) -> Path | None:
    """Locate the on-disk log file for this Application's Burr tracker.

    Reads ``app._tracker`` which is Burr's internal slot for the
    ``LocalTrackingClient``. We pin Burr to a minor version range
    because of this and similar internals (see ``pyproject.toml``).
    Returns ``None`` when the Application has no tracker, or has a
    non-local one, or the resolved path is outside the tracker's
    own storage directory.
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    try:
        storage_dir = Path(tracker.storage_dir).expanduser().resolve()
        log_path = (storage_dir / app.uid / LocalTrackingClient.LOG_FILENAME).resolve()
    except (OSError, AttributeError):
        return None
    # Defence in depth: the computed log path must sit under the tracker's
    # storage dir. If app.uid contained a traversal sequence (it shouldn't,
    # Burr generates UUIDs, but belt-and-braces), refuse to read it.
    try:
        log_path.relative_to(storage_dir)
    except ValueError:
        return None
    return log_path


def _read_trace(path: Path, *, tail: int = _TRACE_MAX_ENTRIES) -> list[dict[str, Any]]:
    """Read a JSONL trace file and return the last ``tail`` records.

    Malformed lines are skipped silently rather than tanking the whole
    response. The cap is in place because Burr's tracker is append-only;
    long-running sessions accumulate; an MCP client doesn't want the
    full 50 MB log returned over the wire on every read.
    """
    entries: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if tail and len(entries) > tail:
        entries = entries[-tail:]
    return entries
