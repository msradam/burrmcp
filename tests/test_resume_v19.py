"""v1.9 additions: app_id surfacing and generalized state_loader.

Every tool response now carries an ``app_id`` field so clients can
track which Burr Application id to remember for cross-restart
resume. ``fork_from_past`` accepts a ``state_loader`` configured at
mount time, letting users plug in custom persisters (SQLite, S3,
postgres, etc.) rather than being restricted to LocalTrackingClient.
"""

from __future__ import annotations

from typing import Any

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.core.persistence import BaseStateLoader
from burr.core.state import State as BurrState
from fastmcp import Client

from burrmcp import ServingMode, mount


@action(reads=[], writes=["counter"])
async def tick(state: State) -> State:
    return state.update(counter=state.get("counter", 0) + 1)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(tick=tick)
        .with_transitions(("tick", "tick"))
        .with_state(counter=0)
        .with_entrypoint("tick")
        .build()
    )


# ── app_id surfacing ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_response_includes_app_id():
    server = mount(_factory, mode=ServingMode.STEP, name="appid-step")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "tick", "inputs": {}})
        out = r.structured_content
        assert "app_id" in out
        assert isinstance(out["app_id"], str)
        assert len(out["app_id"]) > 0


@pytest.mark.asyncio
async def test_reset_session_response_includes_app_id():
    server = mount(_factory, mode=ServingMode.STEP, name="appid-reset")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        r = await client.call_tool("reset_session", {})
        out = r.structured_content
        assert "app_id" in out


@pytest.mark.asyncio
async def test_fork_at_response_includes_app_id():
    server = mount(_factory, mode=ServingMode.STEP, name="appid-fork-at")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        r = await client.call_tool("fork_at", {"sequence_id": 0})
        out = r.structured_content
        assert "app_id" in out


@pytest.mark.asyncio
async def test_app_id_stable_across_session():
    """Multiple step calls in one session report the same app_id."""
    server = mount(_factory, mode=ServingMode.STEP, name="appid-stable")
    async with Client(server) as client:
        ids: list[str] = []
        for _ in range(3):
            r = await client.call_tool("step", {"action": "tick", "inputs": {}})
            out = r.structured_content
            ids.append(out["app_id"])
        assert len(set(ids)) == 1, f"app_id should be stable, saw {ids}"


@pytest.mark.asyncio
async def test_app_id_changes_after_reset():
    """reset_session rebuilds the Application, so the app_id changes."""
    server = mount(_factory, mode=ServingMode.STEP, name="appid-reset-changes")
    async with Client(server) as client:
        r1 = await client.call_tool("step", {"action": "tick", "inputs": {}})
        id_before = r1.structured_content["app_id"]
        r2 = await client.call_tool("reset_session", {})
        id_after = r2.structured_content["app_id"]
        assert id_before != id_after


# ── generalized state_loader ────────────────────────────────────────


class _InMemoryLoader(BaseStateLoader):
    """Minimal BaseStateLoader for tests. Stores PersistedStateData
    in a dict keyed by (partition_key, app_id, sequence_id)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, int], dict[str, Any]] = {}

    def save(
        self,
        partition_key: str,
        app_id: str,
        sequence_id: int,
        state: BurrState,
        position: str,
    ) -> None:
        self._store[(partition_key, app_id, sequence_id)] = {
            "partition_key": partition_key,
            "app_id": app_id,
            "sequence_id": sequence_id,
            "position": position,
            "state": state,
            "created_at": "test-time",
            "status": "completed",
        }

    def load(
        self,
        partition_key: str,
        app_id: str | None,
        sequence_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        if sequence_id is None:
            # Pick the highest sequence_id for this (partition, app_id).
            matching = [
                (k, v) for k, v in self._store.items() if k[0] == partition_key and k[1] == app_id
            ]
            if not matching:
                return None
            return max(matching, key=lambda kv: kv[0][2])[1]
        return self._store.get((partition_key, app_id, sequence_id))

    def list_app_ids(self, partition_key: str, **kwargs: Any) -> list[str]:
        return list({k[1] for k in self._store if k[0] == partition_key})


@pytest.mark.asyncio
async def test_fork_from_past_uses_custom_state_loader():
    """A state_loader passed to mount() wins over the on-disk tracker."""
    loader = _InMemoryLoader()
    # Pre-populate the loader with a saved snapshot at "tick" with counter=42.
    loader.save(
        partition_key="",
        app_id="saved-run-99",
        sequence_id=5,
        state=BurrState({"counter": 42}),
        position="tick",
    )

    server = mount(
        _factory,
        mode=ServingMode.STEP,
        name="custom-loader",
        state_loader=loader,
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "saved-run-99", "sequence_id": 5, "partition_key": ""},
        )
        out = r.structured_content
        assert out["action"] == "fork_from_past"
        assert out["state"]["counter"] == 42
        assert out["result"]["from_action"] == "tick"


@pytest.mark.asyncio
async def test_fork_from_past_state_loader_unknown_id_refuses():
    loader = _InMemoryLoader()
    server = mount(
        _factory,
        mode=ServingMode.STEP,
        name="loader-unknown",
        state_loader=loader,
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "nope", "sequence_id": 0},
        )
        out = r.structured_content
        assert out["error"] == "unknown_past_run"


@pytest.mark.asyncio
async def test_fork_from_past_state_loader_takes_precedence_over_tracker(tmp_path, monkeypatch):
    """If both a state_loader AND a tracker are configured, the
    explicit state_loader wins."""
    from burr.tracking.client import LocalTrackingClient

    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"precedence-test-{tmp_path.name}"

    def tracked_factory():
        return (
            ApplicationBuilder()
            .with_actions(tick=tick)
            .with_transitions(("tick", "tick"))
            .with_tracker(LocalTrackingClient(project=project))
            .with_state(counter=0)
            .with_entrypoint("tick")
            .build()
        )

    # In-memory loader saves a state with counter=999.
    loader = _InMemoryLoader()
    loader.save(
        partition_key="",
        app_id="my-app",
        sequence_id=0,
        state=BurrState({"counter": 999}),
        position="tick",
    )

    server = mount(
        tracked_factory,
        mode=ServingMode.STEP,
        name="precedence",
        state_loader=loader,
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "my-app", "sequence_id": 0},
        )
        out = r.structured_content
        # The 999 came from the loader, not from any tracker file.
        assert out["state"]["counter"] == 999
