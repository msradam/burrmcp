"""Builder-level state forking via ``initialize_from(fork_from_app_id=...)``.

Burr supports forking one Application's state into a new one via
``ApplicationBuilder.initialize_from(persister, fork_from_app_id=...,
fork_from_sequence_id=...)``. Given any prior ``app_id`` (and
optionally a sequence_id), the builder loads that snapshot through
the persister and returns a fresh Application that starts from the
loaded state with its own ``uid``. The two Applications then walk
independently.

This is the Burr-level surface that Theodosia's ``fork_from_past``
meta-tool wraps. ``sqlite_persister`` shows it driven through MCP;
this demo shows it directly at the builder. Tests build a baseline,
then construct alternate Applications via ``build_fork(...)`` that
share the baseline's state at a chosen sequence_id, and walk both
divergently.

Domain: a tiny budget planner. Each ``commit`` action subtracts an
amount from a running budget. The MCP surface lets clients drive a
single budget session; the ``theodosia://forks`` resource exposes every
persister row across that session (and any forks built in-process)
so the agent can see the full ledger.

Run:

    uv run python examples/state_forking.py
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.persistence import BaseStatePersister, PersistedStateData
from burr.core.state import State as BurrState

from theodosia import ServingMode, mount

# == persister =======================================================


class InMemoryPersister(BaseStatePersister):
    """Process-local ``BaseStatePersister`` backed by a dict-of-dicts.

    Persists raw ``State`` instances by ``(partition_key, app_id,
    sequence_id)``. ``load`` returns the latest (or specified)
    snapshot per ``(partition_key, app_id)``.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int], PersistedStateData] = {}

    def initialize(self) -> None:
        return None

    def is_initialized(self) -> bool:
        return True

    def list_app_ids(self, partition_key: str, **_: Any) -> list[str]:
        pk = partition_key or ""
        return sorted({app_id for (p, app_id, _seq) in self._rows if p == pk})

    def save(
        self,
        partition_key: str | None,
        app_id: str,
        sequence_id: int,
        position: str,
        state: BurrState,
        status: Literal["completed", "failed"],
        **_: Any,
    ) -> None:
        self._rows[(partition_key or "", app_id, sequence_id)] = PersistedStateData(
            partition_key=partition_key or "",
            app_id=app_id,
            sequence_id=sequence_id,
            position=position,
            state=state,
            created_at=datetime.now(UTC).isoformat(),
            status=status,
        )

    def load(
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
                "partition_key": row["partition_key"],
                "app_id": row["app_id"],
                "sequence_id": row["sequence_id"],
                "position": row["position"],
                "status": row["status"],
                "state": dict(row["state"].get_all()),
            }
            for row in sorted(
                self._rows.values(),
                key=lambda r: (r["app_id"], r["sequence_id"]),
            )
        ]


# == actions =========================================================


@action(reads=["budget"], writes=["budget", "ledger"])
async def commit(state: State, amount: float, note: str = "") -> State:
    """Subtract ``amount`` from the running budget.

    Declared ``async`` so Burr's ``astep`` path (used by Theodosia)
    captures the post-action state in its ``post_run_step`` hook.
    With a sync action body, Burr's ``astep`` delegates to ``_step``
    and the persister sees the pre-action state at each sequence_id.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    new_budget = float(state["budget"]) - amount
    ledger_entry = {
        "amount": amount,
        "note": note,
        "balance_after": new_budget,
        "at": datetime.now(UTC).isoformat(),
    }
    return state.update(budget=new_budget, ledger=[*state.get("ledger", []), ledger_entry])


# == application factory =============================================


def build_application(*, persister: InMemoryPersister, app_id: str | None = None):
    """Build a fresh budget session. ``app_id`` defaults to a uuid."""
    return (
        ApplicationBuilder()
        .with_identifiers(app_id=app_id or uuid.uuid4().hex)
        .with_actions(commit=commit)
        .with_transitions(("commit", "commit"))
        .with_state_persister(persister)
        .with_state(budget=100.0, ledger=[])
        .with_entrypoint("commit")
        .build()
    )


def build_fork(*, persister: InMemoryPersister, parent_app_id: str, sequence_id: int | None = None):
    """Build a NEW Application that initializes from ``parent_app_id``'s
    state at ``sequence_id`` (latest if None). The new app has its own
    uid; it shares the initial state, then walks independently.
    """
    return (
        ApplicationBuilder()
        .with_identifiers(app_id=uuid.uuid4().hex)
        .with_actions(commit=commit)
        .with_transitions(("commit", "commit"))
        .with_state_persister(persister)
        .initialize_from(
            persister,
            resume_at_next_action=False,
            default_state={"budget": 100.0, "ledger": []},
            default_entrypoint="commit",
            fork_from_app_id=parent_app_id,
            fork_from_sequence_id=sequence_id,
        )
        .build()
    )


# == server ==========================================================


def build_server():
    persister = InMemoryPersister()
    # State lives in a closure: one persister per server process; one
    # baseline Application built lazily on first step.
    holder: dict[str, Any] = {"baseline_id": None}

    def factory():
        app = build_application(persister=persister)
        if holder["baseline_id"] is None:
            holder["baseline_id"] = app.uid
        return app

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="state-forking",
        instructions=(
            "Budget planner FSM. Each commit(amount, note) subtracts "
            "from the running budget. The session uses an in-memory "
            "BaseStatePersister, so every step is recorded under the "
            "current app_id. Read theodosia://forks for the persister "
            "ledger (every saved row across all forks)."
        ),
    )

    @server.resource("theodosia://forks")
    async def _forks_resource() -> str:
        return json.dumps(
            {
                "baseline_app_id": holder["baseline_id"],
                "rows": persister.snapshot(),
            },
            indent=2,
            default=str,
        )

    return server


if __name__ == "__main__":
    build_server().run()
