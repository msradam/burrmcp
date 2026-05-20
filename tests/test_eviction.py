"""Session-store eviction: TTL and max-size both work, lazily.

Eviction happens on access (``store.get_or_create``), not on a timer.
Tests poke the store directly to avoid having to fake the clock through
FastMCP's session machinery.
"""

from __future__ import annotations

import time

import pytest
from coffee_order import build_application

from burrmcp.adapter import _SessionStore


def test_ttl_eviction_drops_stale_entries():
    """An entry older than ttl_seconds is evicted on the next access."""
    store = _SessionStore(ttl_seconds=1, max_sessions=None)
    store.get_or_create("session-a", build_application)
    assert len(store) == 1

    # Backdate the entry so eviction sees it as stale.
    store._entries["session-a"].last_access = time.monotonic() - 10

    # Any access triggers a sweep.
    store.get_or_create("session-b", build_application)
    assert "session-a" not in store._entries
    assert "session-b" in store._entries


def test_max_size_eviction_drops_oldest():
    """When max_sessions is reached, the LRU entry is evicted on insert."""
    store = _SessionStore(ttl_seconds=None, max_sessions=2)
    store.get_or_create("a", build_application)
    # Make 'a' older than 'b' so 'a' is the LRU when we add a third.
    store._entries["a"].last_access = time.monotonic() - 5
    store.get_or_create("b", build_application)

    store.get_or_create("c", build_application)
    assert "a" not in store._entries
    assert {"b", "c"} <= set(store._entries)


def test_no_eviction_when_both_disabled():
    """ttl_seconds=None and max_sessions=None means unbounded."""
    store = _SessionStore(ttl_seconds=None, max_sessions=None)
    for i in range(20):
        store.get_or_create(f"s{i}", build_application)
    assert len(store) == 20


def test_history_survives_recent_access():
    """An entry accessed recently keeps its history across the eviction sweep."""
    store = _SessionStore(ttl_seconds=1, max_sessions=None)
    entry = store.get_or_create("a", build_application)
    entry.history.append({"seq": 0, "action": "x"})

    # Trigger another get; entry is still fresh.
    store.get_or_create("a", build_application)
    assert store.history("a") == [{"seq": 0, "action": "x"}]


def test_evicted_session_starts_fresh_application():
    """After TTL eviction, the next call builds a new Application via the factory."""
    store = _SessionStore(ttl_seconds=1, max_sessions=None)
    entry_v1 = store.get_or_create("a", build_application)
    app_v1 = entry_v1.application
    entry_v1.history.append({"seq": 0, "action": "x"})

    # Force expiry.
    store._entries["a"].last_access = time.monotonic() - 10

    entry_v2 = store.get_or_create("a", build_application)
    app_v2 = entry_v2.application
    assert app_v2 is not app_v1, "eviction should have replaced the Application"
    assert entry_v2.history == [], "fresh entry, no carryover history"


@pytest.mark.asyncio
async def test_history_after_eviction_starts_fresh():
    """End-to-end: a session's history is gone after its entry is evicted."""
    import json

    from fastmcp import Client

    from burrmcp import ServingMode, mount

    server = mount(
        build_application,
        mode=ServingMode.STEP,
        name="evict-coffee",
        session_ttl_seconds=1,
    )

    async with Client(server) as client:
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1

        # Reach into the closure-scoped store and backdate the session.
        # The store is on the FastMCP server's lifespan; we get at it by
        # walking the resource handler's closure. This is white-box, but
        # the eviction unit tests above are the canonical coverage; this
        # one just confirms the wiring end-to-end.
