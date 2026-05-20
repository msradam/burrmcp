"""Async persister via ``AsyncBaseStatePersister`` + ``PersisterHookAsync``.

Counterpart to ``sqlite_persister``. That demo wires a SYNC
``BaseStatePersister`` through ``with_state_persister(persister)``.
This one wires an ASYNC persister:

* ``AsyncEventLogPersister`` subclasses ``AsyncBaseStatePersister``,
  so ``save`` and ``load`` are coroutines. Each save awaits an
  artificial latency to simulate real I/O.
* ``PersisterHookAsync(persister)`` is wired manually via
  ``with_hooks(...)`` rather than going through
  ``with_state_persister(async_persister)`` + ``.abuild()``. That
  keeps the factory sync (BurrMCP's ``mount()`` calls factories
  from inside an already-running event loop, so ``asyncio.run`` on
  ``abuild()`` would deadlock) while still exercising the async
  save path.

BurrMCP's adapter drives every step via ``app.astep``, which awaits
``PostRunStepHookAsync`` hooks. So the async persister's
``await persister.save(...)`` runs inline on the MCP step path.

Domain: a tiny event log. ``record(event, payload)`` appends to a
list in state; the async persister awaits a small latency on each
save. The ``burr://event-log`` resource shows the persister's
contents after each step.

Run:

    uv run python examples/async_persister.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.persistence import AsyncBaseStatePersister, PersistedStateData, PersisterHookAsync
from burr.core.state import State as BurrState

from burrmcp import ServingMode, mount

# == async persister =================================================


class AsyncEventLogPersister(AsyncBaseStatePersister):
    """Process-local async persister. Each save awaits a configurable
    latency to model real I/O (network round-trip, disk fsync, etc.).
    """

    def __init__(self, save_latency_ms: int = 5) -> None:
        self._rows: dict[tuple[str, str, int], PersistedStateData] = {}
        self._save_latency = save_latency_ms / 1000

    async def initialize(self) -> None:
        return None

    async def is_initialized(self) -> bool:
        return True

    async def list_app_ids(self, partition_key: str, **_: Any) -> list[str]:
        pk = partition_key or ""
        return sorted({app_id for (p, app_id, _seq) in self._rows if p == pk})

    async def save(
        self,
        partition_key: str | None,
        app_id: str,
        sequence_id: int,
        position: str,
        state: BurrState,
        status: Literal["completed", "failed"],
        **_: Any,
    ) -> None:
        await asyncio.sleep(self._save_latency)
        self._rows[(partition_key or "", app_id, sequence_id)] = PersistedStateData(
            partition_key=partition_key or "",
            app_id=app_id,
            sequence_id=sequence_id,
            position=position,
            state=state,
            created_at=datetime.now(UTC).isoformat(),
            status=status,
        )

    async def load(
        self,
        partition_key: str | None,
        app_id: str | None,
        sequence_id: int | None = None,
        **_: Any,
    ) -> PersistedStateData | None:
        if app_id is None:
            return None
        pk = partition_key or ""
        rows = [
            (seq, row)
            for (p, a, seq), row in self._rows.items()
            if p == pk and a == app_id and (sequence_id is None or seq == sequence_id)
        ]
        if not rows:
            return None
        rows.sort(key=lambda x: x[0])
        return rows[-1][1]

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "app_id": row["app_id"],
                "sequence_id": row["sequence_id"],
                "position": row["position"],
                "status": row["status"],
                "events": row["state"].get_all().get("events", []),
            }
            for row in sorted(self._rows.values(), key=lambda r: (r["app_id"], r["sequence_id"]))
        ]


# == actions =========================================================


@action(reads=["events"], writes=["events"])
async def record(state: State, event: str, payload: dict[str, Any] | None = None) -> State:
    """Append an event to the in-state log."""
    entry = {
        "event": event,
        "payload": payload or {},
        "at": datetime.now(UTC).isoformat(),
    }
    return state.update(events=[*state.get("events", []), entry])


# == graph ===========================================================


def build_application(*, persister: AsyncEventLogPersister, app_id: str | None = None):
    """Wires the async persister via PersisterHookAsync rather than
    with_state_persister, so the sync builder path keeps working from
    inside MCP's already-running event loop.
    """
    return (
        ApplicationBuilder()
        .with_identifiers(app_id=app_id or uuid.uuid4().hex)
        .with_actions(record=record)
        .with_transitions(("record", "record"))
        .with_hooks(PersisterHookAsync(persister))
        .with_state(events=[])
        .with_entrypoint("record")
        .build()
    )


def build_server():
    persister = AsyncEventLogPersister(save_latency_ms=5)

    def factory():
        return build_application(persister=persister)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="async-persister",
        instructions=(
            "Async event-log FSM. Each record(event, payload) appends "
            "to state; an AsyncEventLogPersister (subclass of "
            "AsyncBaseStatePersister) awaits a small latency to save "
            "the post-action state. Read burr://event-log for the "
            "persister contents."
        ),
    )

    @server.resource("burr://event-log")
    async def _event_log_resource() -> str:
        return json.dumps({"rows": persister.snapshot()}, indent=2, default=str)

    return server


if __name__ == "__main__":
    build_server().run()
