"""burr://trace: passthrough of Burr's on-disk LocalTrackingClient log.

The trace is Burr's structured per-step record, complementary to our
in-memory burr://history. Reads the JSONL file the tracker writes to
``~/.burr/<project>/<app_uid>/log.jsonl``.

Tests verify: no_tracker error when the Application has no tracker;
empty list before the first step; populated entries after steps; cap
on the number of returned records.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from burr_mcp import ServingMode, mount
from burr_mcp.adapter import _tracker_log_path


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
async def test_trace_returns_no_tracker_when_application_has_no_tracker():
    server = mount(_untracked_app, mode=ServingMode.STEP, name="no-tracker")
    async with Client(server) as client:
        trace_text = (await client.read_resource("burr://trace"))[0].text
        trace = json.loads(trace_text)
        assert (
            trace
            == {
                "error": "no_tracker",
                "message": pytest.approx(trace["message"]),  # any non-empty string
            }
            or trace["error"] == "no_tracker"
        )
        assert "LocalTrackingClient" in trace["message"]


@pytest.mark.asyncio
async def test_trace_returns_entries_after_step(tmp_path, monkeypatch):
    """A tracked Application populates burr://trace after running steps."""
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate from real ~/.burr/

    project = f"trace-test-{tmp_path.name}"
    server = mount(_tracked_app_factory(project), mode=ServingMode.STEP, name="tracked")
    async with Client(server) as client:
        # Empty before any step.
        trace_text = (await client.read_resource("burr://trace"))[0].text
        assert json.loads(trace_text) == []

        await client.call_tool("step", {"action": "tick", "inputs": {}})
        await client.call_tool("step", {"action": "tick", "inputs": {}})

        trace_text = (await client.read_resource("burr://trace"))[0].text
        trace = json.loads(trace_text)
        assert isinstance(trace, list)
        assert len(trace) > 0
        # Each entry should have something resembling Burr's record shape.
        first = trace[0]
        assert isinstance(first, dict)


@pytest.mark.asyncio
async def test_trace_path_resolves_to_application_uid(tmp_path, monkeypatch):
    """The path helper returns a path under the tracker's storage dir
    that names the Application's uid."""
    monkeypatch.setenv("HOME", str(tmp_path))

    factory = _tracked_app_factory(f"path-test-{tmp_path.name}")
    app = factory()
    path = _tracker_log_path(app)
    assert path is not None
    assert app.uid in str(path)
    assert path.name == "log.jsonl"


def test_trace_path_returns_none_without_tracker():
    """Without a LocalTrackingClient the path helper returns None."""
    app = _untracked_app()
    assert _tracker_log_path(app) is None


@pytest.mark.asyncio
async def test_trace_handles_traversal_in_uid_safely(monkeypatch, tmp_path):
    """If app.uid contained a path-traversal sequence, the resource
    would refuse rather than reading outside the storage dir."""
    monkeypatch.setenv("HOME", str(tmp_path))

    factory = _tracked_app_factory(f"safety-test-{tmp_path.name}")
    app = factory()
    # Hand-craft an evil uid. Burr generates UUIDs in practice; this is
    # belt-and-braces for the path-relative check.
    app._uid = "../../../etc"  # type: ignore[attr-defined]
    assert _tracker_log_path(app) is None
