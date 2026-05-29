"""Per-session bookkeeping: the lazy TTL + max-size store and one entry.

A session is keyed by FastMCP's ``ctx.session_id``. Each entry owns the
Application instance (factory mode) plus the per-session attempt history,
sub-run records, and an ``asyncio.Lock`` that serializes ``app.astep``
calls within one session. Different sessions still proceed in parallel.

Re-exported from :mod:`theodosia.adapter` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from burr.core import Application

ApplicationFactory = Callable[[], Application]

_DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour idle
_DEFAULT_MAX_SESSIONS = 100


@dataclass
class _SessionEntry:
    """One session's slot in ``_SessionStore``.

    ``application`` is None in shared-app mode (the server has one
    Application that all sessions mutate; per-session apps aren't
    created). ``history`` is always per-session: each session sees
    only the timeline of its own calls.

    ``lock`` serializes ``app.astep`` calls within one session. Burr
    Applications are not thread-safe, and frontier clients can fire
    parallel tool calls within one MCP session (the protocol permits
    it). The lock means concurrent step calls from the same session
    queue rather than racing on the Application's state pointer.
    Different sessions still proceed in parallel.

    ``subruns`` holds the timelines of any sub-Applications spawned
    from inside this session's actions via ``theodosia.spawn_subapp``.
    Each entry has its own id, label, started/ended timestamps,
    history list, and optional final state. Subrun ids are surfaced
    on the parent action's history entry via the ``subruns`` key so
    a client can correlate "the analyse action spawned subrun X" with
    "subrun X had the following timeline."
    """

    application: Application | None
    history: list[dict[str, Any]] = field(default_factory=list)
    subruns: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_access: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _SessionStore:
    """Lazy TTL + max-size session store.

    Eviction is lazy: stale entries are dropped on the next access
    (``get_or_create`` or any of the helpers). No background thread, no
    asyncio task, no timer surprises.

    Defaults are chosen so a small interactive server doesn't notice
    eviction at all. Long-running multi-tenant servers should tune
    ``ttl_seconds`` and ``max_sessions`` based on real session durations
    and memory budgets.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._entries: dict[str, _SessionEntry] = {}
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    def _evict_stale(self) -> None:
        if self.ttl_seconds is None:
            return
        now = time.monotonic()
        stale = [sid for sid, e in self._entries.items() if now - e.last_access > self.ttl_seconds]
        for sid in stale:
            del self._entries[sid]

    def _evict_if_full(self) -> None:
        if self.max_sessions is None:
            return
        while len(self._entries) >= self.max_sessions:
            oldest = min(self._entries, key=lambda s: self._entries[s].last_access)
            del self._entries[oldest]

    def get_or_create(
        self,
        sid: str,
        factory: ApplicationFactory | None,
    ) -> _SessionEntry:
        self._evict_stale()
        entry = self._entries.get(sid)
        if entry is None:
            self._evict_if_full()
            app = factory() if factory is not None else None
            entry = _SessionEntry(application=app)
            self._entries[sid] = entry
        entry.last_access = time.monotonic()
        return entry

    def history(self, sid: str) -> list[dict[str, Any]]:
        entry = self._entries.get(sid)
        return list(entry.history) if entry is not None else []

    def __len__(self) -> int:
        return len(self._entries)
