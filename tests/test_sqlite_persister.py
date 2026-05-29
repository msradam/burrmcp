"""Tests for examples/sqlite_persister.py.

Two layers:

* Persister unit tests exercise ``SQLitePersister`` in isolation: save
  then load round-trips state, ``load(sequence_id=None)`` returns the
  latest row, unknown app_ids return None, ``list_app_ids`` reflects
  what was saved.
* FSM-level tests exercise the persister wired through the
  factory's ``with_state_persister`` and through ``mount(state_loader=...)``.
  The fork_from_past round-trip is the load-bearing one: ticks land in
  SQLite, a fresh server resolves a prior app_id, and state is restored.

All tests use ``tmp_path`` SQLite files; no real-disk persistence
happens in CI scope.
"""

from __future__ import annotations

import sqlite3

import pytest
from burr.core.state import State as BurrState
from fastmcp import Client

# conftest.py inserts examples/ onto sys.path, so this import works.
from sqlite_persister import (
    SQLitePersister,
    build_application,
    build_application_with_resume,
    finalize,
    start,
    tick,
)

from theodosia import ServingMode, mount

# == persister unit tests ==========================================


def test_save_then_load_round_trips_state(tmp_path):
    p = SQLitePersister(str(tmp_path / "demo.db"))
    state = BurrState({"counter": 7, "log": ["a", "b"], "status": "started"})
    p.save(
        partition_key="",
        app_id="run-1",
        sequence_id=3,
        position="tick",
        state=state,
        status="completed",
    )
    loaded = p.load(partition_key="", app_id="run-1", sequence_id=3)
    assert loaded is not None
    assert loaded["partition_key"] == ""
    assert loaded["app_id"] == "run-1"
    assert loaded["sequence_id"] == 3
    assert loaded["position"] == "tick"
    assert loaded["status"] == "completed"
    # State is rehydrated as a Burr State, not a dict.
    assert isinstance(loaded["state"], BurrState)
    assert loaded["state"].get_all() == {
        "counter": 7,
        "log": ["a", "b"],
        "status": "started",
    }


def test_load_without_sequence_id_returns_latest(tmp_path):
    p = SQLitePersister(str(tmp_path / "latest.db"))
    for seq, counter in [(0, 0), (1, 1), (2, 2), (3, 3)]:
        p.save(
            partition_key="",
            app_id="run-latest",
            sequence_id=seq,
            position="tick",
            state=BurrState({"counter": counter}),
            status="completed",
        )
    loaded = p.load(partition_key="", app_id="run-latest", sequence_id=None)
    assert loaded is not None
    assert loaded["sequence_id"] == 3
    assert loaded["state"].get_all()["counter"] == 3


def test_load_unknown_app_id_returns_none(tmp_path):
    p = SQLitePersister(str(tmp_path / "unknown.db"))
    # Save one run, query a different app_id.
    p.save(
        partition_key="",
        app_id="known",
        sequence_id=0,
        position="start",
        state=BurrState({"counter": 0}),
        status="completed",
    )
    assert p.load(partition_key="", app_id="not-known") is None
    assert p.load(partition_key="", app_id="not-known", sequence_id=0) is None
    # Wrong partition_key also returns None.
    assert p.load(partition_key="other-partition", app_id="known") is None


def test_list_app_ids_returns_saved_set(tmp_path):
    p = SQLitePersister(str(tmp_path / "list.db"))
    for app_id in ["a", "b", "c"]:
        p.save(
            partition_key="",
            app_id=app_id,
            sequence_id=0,
            position="start",
            state=BurrState({"counter": 0}),
            status="completed",
        )
    # An unrelated partition shouldn't leak into the listing.
    p.save(
        partition_key="other",
        app_id="z",
        sequence_id=0,
        position="start",
        state=BurrState({"counter": 0}),
        status="completed",
    )
    assert set(p.list_app_ids(partition_key="")) == {"a", "b", "c"}
    assert set(p.list_app_ids(partition_key="other")) == {"z"}
    assert p.list_app_ids(partition_key="empty-partition") == []


def test_save_overwrite_same_key_replaces_row(tmp_path):
    """INSERT OR REPLACE: re-saving (pk, app_id, seq) overwrites."""
    p = SQLitePersister(str(tmp_path / "overwrite.db"))
    for counter in [1, 2, 99]:
        p.save(
            partition_key="",
            app_id="r",
            sequence_id=0,
            position="tick",
            state=BurrState({"counter": counter}),
            status="completed",
        )
    loaded = p.load(partition_key="", app_id="r", sequence_id=0)
    assert loaded is not None
    assert loaded["state"].get_all()["counter"] == 99
    # And only one row landed.
    with sqlite3.connect(p.db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM burr_state").fetchone()
    assert count == 1


# == FSM-level tests ===============================================


def _force_step(app, action_name: str) -> None:
    """Walk one named step regardless of transition order.

    Burr's auto-routing picks the first matching transition, so
    `tick -> tick` would win over `tick -> finalize` forever. The MCP
    adapter solves this by overriding `get_next_action`; tests that walk
    the FSM outside MCP do the same here.
    """
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step()
    finally:
        app.get_next_action = original


def test_factory_persists_every_step_to_sqlite(tmp_path):
    """Walk start -> tick -> tick -> finalize and verify each step
    produced a row in the SQLite db, with the expected (position,
    counter) per sequence_id."""
    db = str(tmp_path / "walk.db")
    app = build_application(db_path=db)
    app_id = app.uid
    for target in ["start", "tick", "tick", "finalize"]:
        _force_step(app, target)

    p = SQLitePersister(db)
    # All four steps should be present.
    rows_by_seq: dict[int, dict] = {}
    for seq in range(4):
        loaded = p.load(partition_key="", app_id=app_id, sequence_id=seq)
        assert loaded is not None, f"missing sequence_id={seq}"
        rows_by_seq[seq] = loaded
    assert rows_by_seq[0]["position"] == "start"
    assert rows_by_seq[0]["state"].get_all()["counter"] == 0
    assert rows_by_seq[1]["position"] == "tick"
    assert rows_by_seq[1]["state"].get_all()["counter"] == 1
    assert rows_by_seq[2]["position"] == "tick"
    assert rows_by_seq[2]["state"].get_all()["counter"] == 2
    assert rows_by_seq[3]["position"] == "finalize"
    assert rows_by_seq[3]["state"].get_all()["final_count"] == 2


def test_factory_independent_runs_get_distinct_app_ids(tmp_path):
    """Two build_application calls produce two distinct app_ids; both
    are visible via list_app_ids on the shared SQLite file."""
    db = str(tmp_path / "twins.db")
    a = build_application(db_path=db)
    b = build_application(db_path=db)
    a.step()
    b.step()
    p = SQLitePersister(db)
    assert {a.uid, b.uid} <= set(p.list_app_ids(partition_key=""))
    assert a.uid != b.uid


@pytest.mark.asyncio
async def test_fork_from_past_restores_state_from_sqlite(tmp_path):
    """Canonical 'session went away, comes back with the same app_id,
    fork_from_past restores state' loop, end to end through mount()."""
    db = str(tmp_path / "fork.db")

    # === past session: tick a few times then drop the app.
    past_app = build_application(db_path=db)
    past_app.step()  # start
    past_app.step()  # tick (counter=1)
    past_app.step()  # tick (counter=2)
    past_app.step()  # tick (counter=3)
    past_app_id = past_app.uid
    assert past_app.state.get("counter") == 3

    # === fresh server, same SQLite file, no in-memory link to past_app.
    def factory():
        return build_application(db_path=db)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="sqlite-fork",
        state_loader=SQLitePersister(db),
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": past_app_id, "sequence_id": -1, "partition_key": ""},
        )
        out = r.structured_content
        assert out["action"] == "fork_from_past"
        assert out["result"]["loaded_app_id"] == past_app_id
        assert out["state"]["counter"] == 3
        # PRIOR_STEP was the last tick, so valid_next includes tick and finalize.
        assert set(out["valid_next_actions"]) >= {"tick", "finalize"}


@pytest.mark.asyncio
async def test_fork_from_past_at_explicit_sequence_id(tmp_path):
    """fork_from_past with an explicit (mid-run) sequence_id picks up
    that specific snapshot, not the latest."""
    db = str(tmp_path / "midfork.db")
    past_app = build_application(db_path=db)
    past_app.step()  # seq 0: start
    past_app.step()  # seq 1: tick, counter=1
    past_app.step()  # seq 2: tick, counter=2
    past_app.step()  # seq 3: tick, counter=3
    past_app_id = past_app.uid

    def factory():
        return build_application(db_path=db)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="sqlite-midfork",
        state_loader=SQLitePersister(db),
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": past_app_id, "sequence_id": 1, "partition_key": ""},
        )
        out = r.structured_content
        assert out["state"]["counter"] == 1
        assert out["result"]["from_action"] == "tick"


@pytest.mark.asyncio
async def test_fork_from_past_unknown_app_id_refuses(tmp_path):
    """Unknown app_id returns the standard unknown_past_run error."""
    db = str(tmp_path / "ghost.db")

    def factory():
        return build_application(db_path=db)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="sqlite-ghost",
        state_loader=SQLitePersister(db),
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "no-such-uid", "sequence_id": -1, "partition_key": ""},
        )
        out = r.structured_content
        assert out["error"] == "unknown_past_run"


# == sanity: the FSM actions themselves still work in isolation ====


def test_actions_pure_logic():
    """A smoke check that the action functions update state as expected
    when called outside an Application. Keeps the action functions
    importable and the test surface independent of Burr's runtime."""
    s = BurrState({"counter": 0, "status": "initial", "log": [], "final_count": None})
    s = start(s)
    assert s.get("status") == "started"
    s = tick(s)
    s = tick(s)
    assert s.get("counter") == 2
    s = finalize(s)
    assert s.get("status") == "finalized"
    assert s.get("final_count") == 2


# == persist-and-resume idiom =====================================


def test_resume_factory_returns_defaults_when_no_prior_state(tmp_path):
    """Fresh db: the factory falls back to default_state / default_entrypoint."""
    db = str(tmp_path / "resume.db")
    app = build_application_with_resume(db_path=db, app_id="new-session")
    assert app.state.get("counter") == 0
    assert app.state.get("status") == "initial"


def test_resume_factory_picks_up_persisted_state(tmp_path):
    """Walk the standard build_application a few steps, then rebuild via
    the resume factory with the same app_id; the new Application starts
    mid-walk, not at zero. Proves save-and-resume round-trip.
    """
    db = str(tmp_path / "resume.db")
    first = build_application(db_path=db)
    first.step()
    first.step()
    walked_counter = first.state.get("counter")
    assert walked_counter >= 1

    resumed = build_application_with_resume(db_path=db, app_id=first.uid)
    assert resumed.state.get("counter") == walked_counter
    assert resumed.state.get("status") == "started"
