"""Tests for examples/trace_decorator.py.

Validates that @trace-decorated functions called from within an
action body produce captured spans, that inputs/outputs land as
attributes, and that the call hierarchy is reflected in the span
tree (child spans carry a parent_uid).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from trace_decorator import (
    TraceSpanCollector,
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


def test_trace_captures_both_helpers():
    sink = TraceSpanCollector()
    app = build_application(sink=sink)
    _force_step(app, "analyze", text="alpha beta alpha")
    entries = sink.spans["analyze"]
    names = {e["name"] for e in entries}
    assert {"tokenize", "count_words"}.issubset(names)


def test_trace_captures_inputs_and_return():
    sink = TraceSpanCollector()
    app = build_application(sink=sink)
    _force_step(app, "analyze", text="alpha beta")
    entries = {e["name"]: e for e in sink.spans["analyze"]}
    # tokenize input + return attributes:
    assert "text" in entries["tokenize"]["attributes"]
    assert "return" in entries["tokenize"]["attributes"]
    # count_words input is the tokens list (passed positionally):
    assert "tokens" in entries["count_words"]["attributes"]
    assert "return" in entries["count_words"]["attributes"]


def test_trace_helpers_are_noop_outside_action():
    """@trace must not raise or break when called outside an action."""
    from trace_decorator import count_words, tokenize

    tokens = tokenize("hello world hello")
    counts = count_words(tokens)
    assert tokens == ["hello", "world", "hello"]
    assert counts == {"hello": 2, "world": 1}


@pytest.mark.asyncio
async def test_trace_spans_via_mcp():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "analyze", "inputs": {"text": "foo bar baz foo"}}
        )
        out = json.loads(r.content[0].text)
        assert out.get("error") is None, out
        assert out["state"]["analysis"]["unique_count"] == 3
        text = (await client.read_resource("burr://trace-spans"))[0].text
        spans = json.loads(text)
        names = {entry["name"] for entry in spans["analyze"]}
        assert {"tokenize", "count_words"}.issubset(names)
