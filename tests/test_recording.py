"""Tests for record-and-replay trajectories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theodosia.testing import (
    FakeUpstream,
    RecordingUpstream,
    ReplayingUpstream,
    ReplayMismatch,
)


@pytest.mark.asyncio
async def test_recording_passes_through_to_wrapped(tmp_path: Path) -> None:
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    rec = RecordingUpstream(fake)
    result = await rec.call("svc", "ping", {"k": "v"})
    assert result == "pong"
    # Recorded entry mirrors the call.
    entries = rec.entries
    assert len(entries) == 1
    assert entries[0]["server"] == "svc"
    assert entries[0]["tool"] == "ping"
    assert entries[0]["args"] == {"k": "v"}
    assert entries[0]["result"] == "pong"


def test_recording_rejects_non_manager() -> None:
    """A wrapped object without ``call`` is a programmer error."""

    class NotAManager:
        pass

    with pytest.raises(TypeError, match="async call"):
        RecordingUpstream(NotAManager())


@pytest.mark.asyncio
async def test_recording_save_and_load_round_trip(tmp_path: Path) -> None:
    fake = FakeUpstream(
        {
            "grafana": {"metrics": {"value": 42}},
            "k8s": {"pods": [{"name": "web-1"}]},
        }
    )
    rec = RecordingUpstream(fake)
    await rec.call("grafana", "metrics", {"m": "cpu"})
    await rec.call("k8s", "pods", {"ns": "default"})
    fixture = tmp_path / "trajectory.jsonl"
    rec.save(fixture)
    # The fixture is JSONL; load it back through the replayer.
    rep = ReplayingUpstream.from_file(fixture)
    assert rep.total == 2
    assert await rep.call("grafana", "metrics", {"m": "cpu"}) == {"value": 42}
    assert await rep.call("k8s", "pods", {"ns": "default"}) == [{"name": "web-1"}]


@pytest.mark.asyncio
async def test_recording_args_defensively_copied() -> None:
    """Mutating the args dict after recording must not corrupt the trace."""
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    rec = RecordingUpstream(fake)
    args = {"k": "original"}
    await rec.call("svc", "ping", args)
    args["k"] = "mutated"
    assert rec.entries[0]["args"] == {"k": "original"}


@pytest.mark.asyncio
async def test_replay_returns_recorded_result_in_order() -> None:
    entries = [
        {"server": "a", "tool": "x", "args": {}, "result": 1},
        {"server": "b", "tool": "y", "args": {}, "result": 2},
    ]
    rep = ReplayingUpstream(entries)
    assert await rep.call("a", "x", {}) == 1
    assert await rep.call("b", "y", {}) == 2


@pytest.mark.asyncio
async def test_replay_drift_on_server_raises() -> None:
    entries = [{"server": "a", "tool": "x", "args": {}, "result": 1}]
    rep = ReplayingUpstream(entries)
    with pytest.raises(ReplayMismatch, match="expected call"):
        await rep.call("b", "x", {})


@pytest.mark.asyncio
async def test_replay_drift_on_tool_raises() -> None:
    entries = [{"server": "a", "tool": "x", "args": {}, "result": 1}]
    rep = ReplayingUpstream(entries)
    with pytest.raises(ReplayMismatch, match="expected call"):
        await rep.call("a", "y", {})


@pytest.mark.asyncio
async def test_replay_drift_on_args_raises_when_strict() -> None:
    entries = [{"server": "a", "tool": "x", "args": {"k": 1}, "result": 1}]
    rep = ReplayingUpstream(entries, strict_args=True)
    with pytest.raises(ReplayMismatch, match="args mismatch"):
        await rep.call("a", "x", {"k": 2})


@pytest.mark.asyncio
async def test_replay_loose_args_skips_args_check() -> None:
    """With strict_args=False, args differences are tolerated.

    This is useful for non-deterministic args like timestamps that aren't
    meaningful for replay.
    """
    entries = [{"server": "a", "tool": "x", "args": {"ts": 100}, "result": 1}]
    rep = ReplayingUpstream(entries, strict_args=False)
    assert await rep.call("a", "x", {"ts": 200}) == 1


@pytest.mark.asyncio
async def test_replay_past_end_raises() -> None:
    rep = ReplayingUpstream([])
    with pytest.raises(ReplayMismatch, match="past end"):
        await rep.call("a", "x", {})


@pytest.mark.asyncio
async def test_replay_progress_counters() -> None:
    entries = [
        {"server": "a", "tool": "x", "args": {}, "result": 1},
        {"server": "a", "tool": "y", "args": {}, "result": 2},
    ]
    rep = ReplayingUpstream(entries)
    assert (rep.consumed, rep.remaining, rep.total) == (0, 2, 2)
    await rep.call("a", "x", {})
    assert (rep.consumed, rep.remaining, rep.total) == (1, 1, 2)
    await rep.call("a", "y", {})
    assert (rep.consumed, rep.remaining, rep.total) == (2, 0, 2)


@pytest.mark.asyncio
async def test_replay_reset_rewinds_cursor() -> None:
    entries = [{"server": "a", "tool": "x", "args": {}, "result": 1}]
    rep = ReplayingUpstream(entries)
    await rep.call("a", "x", {})
    rep.reset()
    assert rep.consumed == 0
    assert await rep.call("a", "x", {}) == 1


def test_replay_from_file_skips_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / "trajectory.jsonl"
    f.write_text(
        json.dumps({"server": "a", "tool": "x", "args": {}, "result": 1})
        + "\n\n"
        + json.dumps({"server": "b", "tool": "y", "args": {}, "result": 2})
        + "\n"
    )
    rep = ReplayingUpstream.from_file(f)
    assert rep.total == 2


def test_replay_server_names_derived_from_entries() -> None:
    entries = [
        {"server": "alpha", "tool": "x", "args": {}, "result": 1},
        {"server": "beta", "tool": "y", "args": {}, "result": 2},
        {"server": "alpha", "tool": "z", "args": {}, "result": 3},
    ]
    rep = ReplayingUpstream(entries)
    assert rep.server_names == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_record_then_replay_round_trip_against_fsm_pattern(tmp_path: Path) -> None:
    """End to end: record a session, save, replay. Replay produces identical
    outputs in the same order, so an FSM run against the replayer sees the
    same trajectory as the original recorded run."""
    upstream = FakeUpstream(
        {
            "grafana": {
                "metrics": {"value": 42},
                "logs": {"lines": ["a", "b"]},
            },
            "k8s": {"pods": [{"name": "web-1"}]},
        }
    )
    rec = RecordingUpstream(upstream)
    # Recording pass: representative trajectory of an investigation.
    await rec.call("grafana", "metrics", {"m": "cpu"})
    await rec.call("grafana", "logs", {"q": "error"})
    await rec.call("k8s", "pods", {"ns": "prod"})
    fixture = tmp_path / "recorded.jsonl"
    rec.save(fixture)
    # Replay pass: same trajectory, no upstream, deterministic.
    rep = ReplayingUpstream.from_file(fixture)
    out = [
        await rep.call("grafana", "metrics", {"m": "cpu"}),
        await rep.call("grafana", "logs", {"q": "error"}),
        await rep.call("k8s", "pods", {"ns": "prod"}),
    ]
    assert out == [
        {"value": 42},
        {"lines": ["a", "b"]},
        [{"name": "web-1"}],
    ]
    assert rep.remaining == 0


def test_recording_manager_works_with_mount() -> None:
    """The recorder satisfies the manager protocol so it can be passed to
    theodosia.mount() in place of the real UpstreamManager."""
    from burr.core import ApplicationBuilder, action

    import theodosia

    @action(reads=[], writes=["x"])
    def begin(state):
        return state.update(x=0)

    app = (
        ApplicationBuilder()
        .with_actions(begin=begin)
        .with_state(x=0)
        .with_entrypoint("begin")
        .build()
    )
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    rec = RecordingUpstream(fake)
    server = theodosia.mount(lambda: app, name="rec-test", upstream=rec)
    assert server.name == "rec-test"
