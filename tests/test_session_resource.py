"""theodosia://session: tracker coordinates for the current MCP session.

Returns ``{project, app_id, app_dir, partition_key}`` so MCP clients
and terminal tooling (``theodosia watch``, scripted inspection, etc.)
can locate the session's tracker data on disk without guessing.
``project`` and ``app_dir`` are null when no ``LocalTrackingClient`` is
attached; ``app_id`` is always populated.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from theodosia import ServingMode, mount


@action(reads=[], writes=["counter"])
def tick(state: State) -> State:
    return state.update(counter=state.get("counter", 0) + 1)


def _tracked_app_factory(project_name: str):
    def factory():
        return (
            ApplicationBuilder()
            .with_actions(tick=tick)
            .with_tracker(LocalTrackingClient(project=project_name))
            .with_transitions(("tick", "tick"))
            .with_state(counter=0)
            .with_entrypoint("tick")
            .build()
        )

    return factory


def _untracked_app():
    return (
        ApplicationBuilder()
        .with_actions(tick=tick)
        .with_transitions(("tick", "tick"))
        .with_state(counter=0)
        .with_entrypoint("tick")
        .build()
    )


@pytest.mark.asyncio
async def test_session_returns_tracker_coords_when_tracker_attached(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"session-test-{tmp_path.name}"
    server = mount(
        _tracked_app_factory(project),
        mode=ServingMode.STEP,
        name="session-tracked",
    )
    async with Client(server) as client:
        text = (await client.read_resource("theodosia://session"))[0].text
        info = json.loads(text)
        assert info["project"] == project
        assert isinstance(info["app_id"], str) and len(info["app_id"]) > 0
        assert info["app_dir"] is not None
        assert project in info["app_dir"]
        assert info["app_id"] in info["app_dir"]
        # partition_key key is always present; default is None.
        assert "partition_key" in info


@pytest.mark.asyncio
async def test_session_returns_nulls_when_no_tracker():
    server = mount(_untracked_app, mode=ServingMode.STEP, name="session-no-tracker")
    async with Client(server) as client:
        text = (await client.read_resource("theodosia://session"))[0].text
        info = json.loads(text)
        assert info["project"] is None
        assert info["app_dir"] is None
        assert isinstance(info["app_id"], str) and len(info["app_id"]) > 0
        assert "partition_key" in info


@pytest.mark.asyncio
async def test_session_app_id_matches_step_response(tmp_path, monkeypatch):
    """The app_id in theodosia://session matches what step() reports for the
    same session. Sanity check that they reference the same Application."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"session-match-{tmp_path.name}"
    server = mount(
        _tracked_app_factory(project),
        mode=ServingMode.STEP,
        name="session-match",
    )
    async with Client(server) as client:
        session_text = (await client.read_resource("theodosia://session"))[0].text
        session_info = json.loads(session_text)
        step_result = await client.call_tool("step", {"action": "tick"})
        step_payload = step_result.structured_content
        assert step_payload["app_id"] == session_info["app_id"]


@pytest.mark.asyncio
async def test_step_response_carries_tracker_project(tmp_path, monkeypatch):
    """Every step response includes tracker_project so collapsed
    tool-result views in MCP clients still surface the project name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = f"step-tracker-{tmp_path.name}"
    server = mount(
        _tracked_app_factory(project),
        mode=ServingMode.STEP,
        name="step-tracker",
    )
    async with Client(server) as client:
        result = await client.call_tool("step", {"action": "tick"})
        payload = result.structured_content
        assert payload["tracker_project"] == project
        assert "app_id" in payload


@pytest.mark.asyncio
async def test_step_response_tracker_project_null_without_tracker():
    """Untracked Application reports tracker_project: null on step."""
    server = mount(_untracked_app, mode=ServingMode.STEP, name="step-no-tracker")
    async with Client(server) as client:
        result = await client.call_tool("step", {"action": "tick"})
        payload = result.structured_content
        assert payload["tracker_project"] is None
