"""fork_from_past: resume a Burr run from on-disk tracker logs.

Lets an agent recover after a server restart (track the app_id on the
client, restore via fork_from_past after reconnect) or fork from any
past persisted run, not just the current session's in-memory history.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from burrmcp import ServingMode, mount


@action(reads=[], writes=["counter"])
async def tick(state: State) -> State:
    return state.update(counter=state.get("counter", 0) + 1)


def _build_tracked_factory(project: str):
    def factory():
        return (
            ApplicationBuilder()
            .with_actions(tick=tick)
            .with_transitions(("tick", "tick"))
            .with_tracker(LocalTrackingClient(project=project))
            .with_state(counter=0)
            .with_entrypoint("tick")
            .build()
        )

    return factory


def _untracked_factory():
    return (
        ApplicationBuilder()
        .with_actions(tick=tick)
        .with_transitions(("tick", "tick"))
        .with_state(counter=0)
        .with_entrypoint("tick")
        .build()
    )


@pytest.mark.asyncio
async def test_fork_from_past_in_meta_tools():
    """fork_from_past is advertised in burr://graph alongside fork_at."""
    server = mount(_untracked_factory, mode=ServingMode.STEP, name="ffp-meta")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("burr://graph"))[0].text)
        meta_names = [m["name"] for m in graph["meta_tools"]]
        assert "fork_from_past" in meta_names


@pytest.mark.asyncio
async def test_fork_from_past_resumes_state_from_disk(tmp_path, monkeypatch):
    """Run a session that ticks the counter three times, capture its
    app_id, then in a fresh session call fork_from_past with that
    app_id and verify state.counter == 3."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"resume-test-{tmp_path.name}"
    factory = _build_tracked_factory(project)

    # Run a "past" session that ticks three times.
    past_app = factory()
    for _ in range(3):
        await past_app.astep()
    past_app_id = past_app.uid
    assert past_app.state.get("counter") == 3

    # Now mount a server using the same factory and resume.
    server = mount(factory, mode=ServingMode.STEP, name="ffp-resume")
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": past_app_id, "sequence_id": -1},
        )
        out = json.loads(r.content[0].text)
        assert out["action"] == "fork_from_past"
        assert out["state"]["counter"] == 3
        assert out["result"]["loaded_app_id"] == past_app_id
        # tick is a self-loop; valid_next from PRIOR_STEP=tick is [tick].
        assert out["valid_next_actions"] == ["tick"]

        # Continue from there.
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["counter"] == 4


@pytest.mark.asyncio
async def test_fork_from_past_refuses_unknown_app_id(tmp_path, monkeypatch):
    """An app_id with no on-disk trace returns unknown_past_run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"unknown-test-{tmp_path.name}"
    server = mount(
        _build_tracked_factory(project),
        mode=ServingMode.STEP,
        name="ffp-unknown",
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "nope-not-a-real-uid", "sequence_id": -1},
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "unknown_past_run"


@pytest.mark.asyncio
async def test_fork_from_past_refuses_without_tracker(tmp_path, monkeypatch):
    """An Application without LocalTrackingClient can't fork from a
    past on-disk run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    server = mount(_untracked_factory, mode=ServingMode.STEP, name="ffp-no-tracker")
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "anything", "sequence_id": -1},
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "no_tracker"


@pytest.mark.asyncio
async def test_fork_from_past_refuses_in_shared_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"shared-test-{tmp_path.name}"
    shared = _build_tracked_factory(project)()
    server = mount(shared, mode=ServingMode.STEP, name="ffp-shared")
    async with Client(server) as client:
        r = await client.call_tool(
            "fork_from_past",
            {"app_id": "anything", "sequence_id": -1},
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "fork_not_supported"


@pytest.mark.asyncio
async def test_fork_from_past_records_in_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"history-test-{tmp_path.name}"
    factory = _build_tracked_factory(project)

    # Persist a past run.
    past_app = factory()
    await past_app.astep()
    past_app_id = past_app.uid

    server = mount(factory, mode=ServingMode.STEP, name="ffp-history")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        await client.call_tool(
            "fork_from_past",
            {"app_id": past_app_id, "sequence_id": -1},
        )
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == ["tick", "fork_from_past"]
        fork_entry = history[1]
        assert fork_entry["inputs"]["app_id"] == past_app_id
        assert fork_entry["refused"] is False
