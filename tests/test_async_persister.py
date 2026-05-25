"""Tests for examples/async_persister.py.

Verifies that AsyncBaseStatePersister + PersisterHookAsync persist
post-action state when an Application is driven via astep, and that
the path works through Theodosia's step tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from async_persister import (
    AsyncEventLogPersister,
    build_application,
    build_server,
)


async def _aforce_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        await app.astep(inputs=inputs or None)
    finally:
        app.get_next_action = original


@pytest.mark.asyncio
async def test_async_persister_records_each_step():
    persister = AsyncEventLogPersister(save_latency_ms=0)
    app = build_application(persister=persister, app_id="A")
    await _aforce_step(app, "record", event="login", payload={"user": "alice"})
    await _aforce_step(app, "record", event="logout", payload={"user": "alice"})
    rows = persister.snapshot()
    assert {r["app_id"] for r in rows} == {"A"}
    assert {r["sequence_id"] for r in rows} == {0, 1}
    # Each row's events should reflect post-action state at that seq.
    by_seq = {r["sequence_id"]: r for r in rows}
    assert len(by_seq[0]["events"]) == 1
    assert by_seq[0]["events"][0]["event"] == "login"
    assert len(by_seq[1]["events"]) == 2
    assert by_seq[1]["events"][1]["event"] == "logout"


@pytest.mark.asyncio
async def test_async_persister_load_returns_latest_state():
    persister = AsyncEventLogPersister(save_latency_ms=0)
    app = build_application(persister=persister, app_id="A")
    await _aforce_step(app, "record", event="login")
    await _aforce_step(app, "record", event="purchase")
    loaded = await persister.load(partition_key=None, app_id="A")
    assert loaded is not None
    assert loaded["sequence_id"] == 1
    events = loaded["state"].get_all()["events"]
    assert [e["event"] for e in events] == ["login", "purchase"]


@pytest.mark.asyncio
async def test_async_persister_load_at_specific_sequence_id():
    persister = AsyncEventLogPersister(save_latency_ms=0)
    app = build_application(persister=persister, app_id="A")
    await _aforce_step(app, "record", event="A")
    await _aforce_step(app, "record", event="B")
    loaded = await persister.load(partition_key=None, app_id="A", sequence_id=0)
    assert loaded is not None
    events = loaded["state"].get_all()["events"]
    assert [e["event"] for e in events] == ["A"]


@pytest.mark.asyncio
async def test_async_persister_via_mcp_step():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "record", "inputs": {"event": "via-mcp"}})
        text = (await client.read_resource("theodosia://event-log"))[0].text
        payload = json.loads(text)
        assert len(payload["rows"]) == 1
        assert payload["rows"][0]["events"][0]["event"] == "via-mcp"
