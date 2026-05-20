"""Tests for examples/state_forking.py.

Validates that ApplicationBuilder.initialize_from(fork_from_app_id=...,
fork_from_sequence_id=...) restores state from a prior session under
a NEW app_id and that the two Applications walk independently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from state_forking import (
    InMemoryPersister,
    build_application,
    build_fork,
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
async def test_persister_records_every_step():
    persister = InMemoryPersister()
    app = build_application(persister=persister, app_id="baseline")
    await _aforce_step(app, "commit", amount=10, note="rent")
    await _aforce_step(app, "commit", amount=20, note="groceries")
    rows = persister.snapshot()
    app_ids = {r["app_id"] for r in rows}
    assert app_ids == {"baseline"}
    assert {r["sequence_id"] for r in rows} == {0, 1}
    final_balances = [r["state"]["budget"] for r in rows]
    assert 90.0 in final_balances and 70.0 in final_balances


@pytest.mark.asyncio
async def test_fork_starts_from_parent_snapshot_with_new_uid():
    persister = InMemoryPersister()
    baseline = build_application(persister=persister, app_id="baseline")
    await _aforce_step(baseline, "commit", amount=10, note="A")
    await _aforce_step(baseline, "commit", amount=20, note="B")

    fork = build_fork(persister=persister, parent_app_id="baseline")
    assert fork.uid != baseline.uid
    assert fork.state["budget"] == 70.0
    assert [e["note"] for e in fork.state["ledger"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_fork_at_specific_sequence_id():
    persister = InMemoryPersister()
    baseline = build_application(persister=persister, app_id="baseline")
    await _aforce_step(baseline, "commit", amount=10, note="A")  # seq 0 -> 90.
    await _aforce_step(baseline, "commit", amount=20, note="B")  # seq 1 -> 70.
    await _aforce_step(baseline, "commit", amount=30, note="C")  # seq 2 -> 40.

    fork = build_fork(persister=persister, parent_app_id="baseline", sequence_id=0)
    assert fork.state["budget"] == 90.0
    assert [e["note"] for e in fork.state["ledger"]] == ["A"]


@pytest.mark.asyncio
async def test_fork_walks_independently_of_parent():
    persister = InMemoryPersister()
    baseline = build_application(persister=persister, app_id="baseline")
    await _aforce_step(baseline, "commit", amount=10, note="A")

    fork = build_fork(persister=persister, parent_app_id="baseline")
    await _aforce_step(fork, "commit", amount=50, note="alt-big-spend")
    await _aforce_step(baseline, "commit", amount=5, note="A-frugal")

    assert fork.state["budget"] == 40.0
    assert baseline.state["budget"] == 85.0


@pytest.mark.asyncio
async def test_persister_visible_via_mcp_resource():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "commit", "inputs": {"amount": 25, "note": "via-mcp"}}
        )
        text = (await client.read_resource("burr://forks"))[0].text
        payload = json.loads(text)
        assert payload["baseline_app_id"] is not None
        assert any(r["state"]["budget"] == 75.0 for r in payload["rows"])
