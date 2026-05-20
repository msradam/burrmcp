"""A custom BaseStateLoader/BaseStateSaver plugged into mount(state_loader=...).

Every other persistence-flavored example in this repo relies on Burr's
built-in ``LocalTrackingClient`` to write JSONL log files under
``~/.burr/``. That works, but it is not the only contract. Burr exposes
``BaseStateLoader`` / ``BaseStateSaver`` / ``BaseStatePersister`` as the
canonical interfaces any backend (SQLite, Postgres, S3, Redis, etc.)
must implement to participate in state persistence. See:

    https://github.com/apache/burr/blob/main/burr/core/persistence.py

This example wires a minimal SQLite-backed ``BaseStatePersister`` into
two places:

1. The factory: ``ApplicationBuilder().with_state_persister(persister)``
   so Burr's ``PersisterHook`` calls ``persister.save(...)`` after every
   step. State lands in a real SQLite file on disk, not in-memory.
2. The mount: ``mount(..., state_loader=persister)`` so the burrmcp
   server's ``fork_from_past`` meta-tool resolves through the same
   persister. The canonical "session went away, a new session comes
   back with the same app_id, ``fork_from_past`` restores the state"
   loop now runs against the SQLite db rather than tracker JSONL.

The FSM itself is intentionally tiny: ``start -> tick -> tick -> ... ->
finalize``. The point is not the workload, the point is that every step
hits the persister and that ``fork_from_past`` can rebuild the session
state from any (app_id, sequence_id) row in the SQLite table.

PersistedStateData (the dict shape ``load`` returns) is a TypedDict
declared in burr.core.persistence with keys: ``partition_key``,
``app_id``, ``sequence_id``, ``position``, ``state``, ``created_at``,
``status``. ``state`` is a ``burr.core.state.State`` instance, not a
plain dict, so the loader parses the JSON column back into a ``State``
on the way out.

Run:

    python examples/sqlite_persister.py

Or wire into a client via ``mount`` and call the ``fork_from_past``
meta-tool with ``{"app_id": <prior-uid>, "sequence_id": <int|-1>}``.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.core.persistence import BaseStatePersister, PersistedStateData
from burr.core.state import State as BurrState

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "sqlite-persister-demo"


# == persister =====================================================


class SQLitePersister(BaseStatePersister):
    """A minimal ``BaseStatePersister`` backed by a single SQLite file.

    Schema (one table):

        CREATE TABLE IF NOT EXISTS burr_state (
            partition_key TEXT NOT NULL,
            app_id        TEXT NOT NULL,
            sequence_id   INTEGER NOT NULL,
            position      TEXT NOT NULL,
            state_json    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            status        TEXT NOT NULL,
            PRIMARY KEY (partition_key, app_id, sequence_id)
        );

    ``state`` is serialised to JSON on write and rehydrated into a
    ``burr.core.state.State`` on read. The position column carries the
    action name (Burr calls this ``position`` in the
    ``PersistedStateData`` TypedDict).

    A ``NULL`` partition_key is stored as the empty string ``""``,
    matching Burr's default of an empty partition. Tests rely on this.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # Make sure the parent directory exists before sqlite3 opens
        # the file. sqlite3 will create the file itself but not its
        # parent directory.
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        # ``check_same_thread=False`` is fine here because Burr's
        # PersisterHook may invoke save() from a worker thread.
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS burr_state (
                    partition_key TEXT NOT NULL,
                    app_id        TEXT NOT NULL,
                    sequence_id   INTEGER NOT NULL,
                    position      TEXT NOT NULL,
                    state_json    TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    PRIMARY KEY (partition_key, app_id, sequence_id)
                )
                """
            )
            conn.commit()

    @staticmethod
    def _normalise_pk(partition_key: str | None) -> str:
        return "" if partition_key is None else partition_key

    def save(
        self,
        partition_key: str | None,
        app_id: str,
        sequence_id: int,
        position: str,
        state: State,
        status: Literal["completed", "failed"],
        **kwargs: Any,
    ) -> None:
        """Persist a single state snapshot to SQLite.

        Signature matches Burr's ``BaseStateSaver.save`` exactly so
        Burr's ``PersisterHook`` can drive it directly. We
        ``INSERT OR REPLACE`` so a re-saved (partition, app_id,
        sequence_id) row simply overwrites; this matches what
        ``LocalTrackingClient`` does under retry.
        """
        pk = self._normalise_pk(partition_key)
        state_json = json.dumps(state.get_all(), default=_json_default)
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO burr_state
                    (partition_key, app_id, sequence_id, position,
                     state_json, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pk, app_id, sequence_id, position, state_json, created_at, status),
            )
            conn.commit()

    def load(
        self,
        partition_key: str,
        app_id: str | None,
        sequence_id: int | None = None,
        **kwargs: Any,
    ) -> PersistedStateData | None:
        """Read a single PersistedStateData row.

        ``sequence_id=None`` returns the latest row for (partition, app_id),
        matching Burr's documented contract. An unknown ``app_id``
        returns ``None`` rather than raising; the burrmcp adapter
        translates ``None`` into the ``unknown_past_run`` error.
        """
        if app_id is None:
            return None
        pk = self._normalise_pk(partition_key)
        with self._connect() as conn:
            if sequence_id is None:
                row = conn.execute(
                    """
                    SELECT partition_key, app_id, sequence_id, position,
                           state_json, created_at, status
                    FROM burr_state
                    WHERE partition_key = ? AND app_id = ?
                    ORDER BY sequence_id DESC
                    LIMIT 1
                    """,
                    (pk, app_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT partition_key, app_id, sequence_id, position,
                           state_json, created_at, status
                    FROM burr_state
                    WHERE partition_key = ? AND app_id = ?
                      AND sequence_id = ?
                    """,
                    (pk, app_id, sequence_id),
                ).fetchone()
        if row is None:
            return None
        loaded_pk, loaded_app_id, loaded_seq, position, state_json, created_at, status = row
        return PersistedStateData(
            partition_key=loaded_pk,
            app_id=loaded_app_id,
            sequence_id=loaded_seq,
            position=position,
            state=BurrState(json.loads(state_json)),
            created_at=created_at,
            status=status,
        )

    def list_app_ids(self, partition_key: str, **kwargs: Any) -> list[str]:
        pk = self._normalise_pk(partition_key)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT app_id FROM burr_state WHERE partition_key = ?",
                (pk,),
            ).fetchall()
        return [r[0] for r in rows]


def _json_default(value: Any) -> Any:
    """Best-effort coercion of non-JSON-serializable state values.

    The toy FSM here only stores ints, strings, and lists, so this is
    unlikely to trigger; it exists so a user adapting this example to
    a richer state shape gets a useful error instead of a stack trace
    pointing at ``json.dumps``.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# == FSM actions ===================================================


@action(reads=[], writes=["counter", "status", "log"])
def start(state: State) -> State:
    """Reset the counter and mark the run started."""
    return state.update(
        counter=0,
        status="started",
        log=["started"],
    )


@action(reads=["counter", "log"], writes=["counter", "log"])
def tick(state: State) -> State:
    """Increment the counter. Each call is one persisted step."""
    next_counter = state["counter"] + 1
    return state.update(
        counter=next_counter,
        log=[*state["log"], f"tick {next_counter}"],
    )


@action(reads=["counter", "log"], writes=["status", "log", "final_count"])
def finalize(state: State) -> State:
    """Terminal step; freezes the final count."""
    return state.update(
        status="finalized",
        final_count=state["counter"],
        log=[*state["log"], f"finalized at {state['counter']}"],
    )


# == graph =========================================================


_TICK_OPEN = Condition.expr("status == 'started'")


def _default_db_path() -> str:
    """Fall back to a tempfile-rooted db when no path is passed.

    Real callers should pick a stable location (``~/.burrmcp/...``) so
    state survives across processes. The tempfile default exists so the
    demo is runnable without any extra setup, and so the doctor command
    can import-and-build the factory cleanly.
    """
    return str(Path(tempfile.gettempdir()) / "burrmcp-sqlite-persister-demo.db")


def build_application(db_path: str | None = None):
    """Construct the demo Application wired to ``SQLitePersister``.

    Args:
        db_path: SQLite file path. Defaults to a stable tempfile path
            so repeated calls in the same process see each other's
            writes. Tests pass an explicit ``tmp_path``-rooted path so
            no real disk state is touched.
    """
    persister = SQLitePersister(db_path or _default_db_path())
    return (
        ApplicationBuilder()
        .with_actions(
            start=start,
            tick=tick,
            finalize=finalize,
        )
        .with_transitions(
            ("start", "tick"),
            ("tick", "tick", _TICK_OPEN),
            ("tick", "finalize", _TICK_OPEN),
        )
        .with_state_persister(persister)
        .with_state(
            counter=0,
            status="initial",
            log=[],
            final_count=None,
        )
        .with_entrypoint("start")
        .build()
    )


def build_server():
    """Mount the demo. ``state_loader`` is the same persister type the
    factory writes through, so ``fork_from_past`` round-trips against
    the same SQLite file.
    """
    db_path = _default_db_path()
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="sqlite-persister",
        state_loader=SQLitePersister(db_path),
        instructions=(
            "Tiny counter FSM persisted to SQLite via a custom "
            "BaseStatePersister. Walk: start -> tick (repeat) -> "
            "finalize. Every step writes a row keyed by (partition_key, "
            "app_id, sequence_id) into the SQLite db. Call "
            "fork_from_past with a remembered app_id and sequence_id "
            "(or -1 for latest) to resume a prior run across server "
            "restarts. The same persister instance backs both writes "
            "(via the factory's with_state_persister) and reads (via "
            "mount(state_loader=...))."
        ),
    )


if __name__ == "__main__":
    build_server().run()
