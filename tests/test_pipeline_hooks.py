"""Tests for examples/pipeline_hooks.py.

Validates that user-defined Burr lifecycle hooks (PreRunStepHook +
PostRunStepHook) fire correctly through ``mount()`` without any
adapter awareness, and that the data they capture round-trips
through a custom ``burr://timings`` resource added on top of the
mounted FastMCP server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from pipeline_hooks import (
    StepCounter,
    TimingHook,
    build_application,
    build_server,
)


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


# == direct hook tests =============================================


def test_timing_hook_records_one_duration_per_action():
    timing = TimingHook()
    app = build_application(timing_hook=timing)
    _force_step(app, "ingest", batch_size=4)
    _force_step(app, "enrich")
    _force_step(app, "aggregate")
    _force_step(app, "finalize")
    snap = timing.snapshot()
    assert set(snap.keys()) == {"ingest", "enrich", "aggregate", "finalize"}
    for name, info in snap.items():
        assert info["runs"] == 1, f"{name}: {info}"
        assert info["total_ms"] >= 0
        assert info["avg_ms"] >= 0


def test_step_counter_increments_per_successful_step():
    counter = StepCounter()
    app = build_application(counter_hook=counter)
    _force_step(app, "ingest", batch_size=4)
    _force_step(app, "enrich")
    _force_step(app, "aggregate")
    assert counter.total_steps == 3
    assert counter.errors == 0


def test_post_run_step_sees_exception():
    """When an action raises, post_run_step is still called with the
    exception populated; StepCounter should classify it as an error."""
    timing = TimingHook()
    counter = StepCounter()
    app = build_application(timing_hook=timing, counter_hook=counter)

    with pytest.raises(ValueError):
        # ingest enforces batch_size >= 1; 0 raises.
        _force_step(app, "ingest", batch_size=0)
    assert counter.total_steps == 0
    assert counter.errors == 1


def test_both_hooks_fire_independently():
    """Burr applies multiple hooks in registration order; both should
    see every step."""
    timing = TimingHook()
    counter = StepCounter()
    app = build_application(timing_hook=timing, counter_hook=counter)
    _force_step(app, "ingest", batch_size=8)
    _force_step(app, "enrich")
    _force_step(app, "aggregate")
    _force_step(app, "finalize")
    assert counter.total_steps == 4
    assert sum(info["runs"] for info in timing.snapshot().values()) == 4


def test_no_hooks_when_none_passed():
    """Sanity: with no hooks the application still builds + runs."""
    app = build_application()
    _force_step(app, "ingest", batch_size=2)
    _force_step(app, "enrich")
    assert app.state["stage"] == "enriched"


# == hook plumbing through mount() =================================


@pytest.mark.asyncio
async def test_hook_fires_through_mcp_step():
    """Drive the pipeline via MCP; the server-scoped hooks should
    record every step including the ones invoked through `step`."""
    server = build_server()
    async with Client(server) as client:
        for action_name, inputs in [
            ("ingest", {"batch_size": 6}),
            ("enrich", {}),
            ("aggregate", {}),
            ("finalize", {}),
        ]:
            r = await client.call_tool("step", {"action": action_name, "inputs": inputs})
            out = json.loads(r.content[0].text)
            assert out.get("error") is None, f"step {action_name} failed: {out}"

        text = (await client.read_resource("burr://timings"))[0].text
        timings = json.loads(text)
        assert timings["errors"] == 0
        assert timings["total_steps"] >= 4
        by_action = timings["by_action"]
        for name in ("ingest", "enrich", "aggregate", "finalize"):
            assert name in by_action, f"missing {name} in {by_action}"
            assert by_action[name]["runs"] >= 1


@pytest.mark.asyncio
async def test_full_walk_produces_summary():
    """End-to-end: the pipeline produces a summary by category."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "ingest", "inputs": {"batch_size": 10}})
        await client.call_tool("step", {"action": "enrich", "inputs": {}})
        await client.call_tool("step", {"action": "aggregate", "inputs": {}})
        r = await client.call_tool("step", {"action": "finalize", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["stage"] == "done"
        summary = out["state"]["summary"]
        # Each batch of 10 produces deterministic high/low buckets via
        # (i*7) % 13 vs threshold 6; just sanity-check both buckets show.
        assert "high" in summary
        assert summary["high"] > 0
