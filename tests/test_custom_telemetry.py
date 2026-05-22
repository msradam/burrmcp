"""Tests for examples/custom_telemetry.py.

Validates that PreStartSpanHook / PostEndSpanHook / DoLogAttributeHook
fire when an action uses Burr's __tracer parameter, and that the
custom SpanCollector hook plugs through mount() unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from custom_telemetry import (
    SpanCollector,
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


def test_span_collector_records_three_spans_per_call():
    sink = SpanCollector()
    app = build_application(sink=sink)
    _force_step(app, "render_report", title="Weekly")
    entries = sink.spans["render_report"]
    assert len(entries) == 3
    assert {e["name"] for e in entries} == {"fetch", "render", "summarize"}


def test_span_collector_captures_logged_attributes():
    sink = SpanCollector()
    app = build_application(sink=sink)
    _force_step(app, "render_report", title="Weekly")
    by_name = {e["name"]: e for e in sink.spans["render_report"]}
    assert by_name["fetch"]["attributes"] == {"source": "memory", "item_count": 3}
    assert by_name["render"]["attributes"] == {"title": "Weekly", "items": 3}
    assert by_name["summarize"]["attributes"] == {"summary": "Weekly: 3 items"}


def test_multiple_calls_accumulate_entries():
    sink = SpanCollector()
    app = build_application(sink=sink)
    _force_step(app, "render_report", title="A")
    app2 = build_application(sink=sink)
    _force_step(app2, "render_report", title="B")
    # Three spans per call, two calls -> six total.
    assert len(sink.spans["render_report"]) == 6


@pytest.mark.asyncio
async def test_spans_resource_via_mcp():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "render_report", "inputs": {"title": "Daily"}}
        )
        out = r.structured_content
        assert out.get("error") is None, out
        text = (await client.read_resource("burr://spans"))[0].text
        spans = json.loads(text)
        names = {entry["name"] for entry in spans["render_report"]}
        assert names == {"fetch", "render", "summarize"}
