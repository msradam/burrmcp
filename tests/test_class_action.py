"""Tests for examples/class_action.py.

Validates that a class subclassing ``burr.core.Action`` (with
explicit ``run`` + ``update`` + ``reads`` + ``writes`` + ``inputs``)
plugs through mount() identically to a ``@action``-decorated function,
and that two instances of the same class can live in one FSM with
different configurations.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

from theodosia import ServingMode, mount

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from class_action import (
    _DEEP_RULES,
    _SHALLOW_RULES,
    QualityCheckAction,
    build_application,
)


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_class_action_carries_per_instance_config():
    """Two instances of QualityCheckAction must have independent
    rule lists + writes keys."""
    a = QualityCheckAction(_SHALLOW_RULES, violations_key="shallow_violations")
    b = QualityCheckAction(_DEEP_RULES, violations_key="deep_violations")
    assert a.writes == ["shallow_violations"]
    assert b.writes == ["deep_violations"]
    assert a._rules is not b._rules


def test_class_action_implements_required_abstract_methods():
    """Sanity-check the Action protocol surface."""
    a = QualityCheckAction(_SHALLOW_RULES, violations_key="x")
    # Properties.
    assert a.reads == ["batch"]
    assert a.writes == ["x"]
    assert a.inputs == []
    # Runnable.
    from burr.core.state import State

    state = State({"batch": [{"id": 1, "value": 10}]})
    result = a.run(state)
    assert result == {"violations": []}
    new_state = a.update(result, state)
    assert new_state["x"] == []


def test_full_walk_on_clean_batch():
    app = build_application()
    _force_step(
        app,
        "ingest",
        batch=[{"id": i, "value": i * 5} for i in range(5)],
    )
    _force_step(app, "shallow_check")
    _force_step(app, "deep_check")
    _force_step(app, "finalize")
    assert app.state["shallow_violations"] == []
    assert app.state["deep_violations"] == []
    assert app.state["verdict"] == "clean"


def test_deep_check_catches_what_shallow_misses():
    """The two instances of QualityCheckAction have different rule
    lists; only deep_check enforces value-range + duplicate rules."""
    app = build_application()
    # All have ids (shallow passes), but values out of range and
    # there's a duplicate id.
    _force_step(
        app,
        "ingest",
        batch=[
            {"id": 1, "value": 200},  # out of range
            {"id": 1, "value": 50},  # duplicate id
        ],
    )
    _force_step(app, "shallow_check")
    _force_step(app, "deep_check")
    _force_step(app, "finalize")
    assert app.state["shallow_violations"] == []
    assert len(app.state["deep_violations"]) >= 2
    assert "flagged" in app.state["verdict"]


def test_shallow_catches_missing_id():
    """When events miss the id field, even the shallow check catches it."""
    app = build_application()
    _force_step(app, "ingest", batch=[{"value": 10}, {"value": 20}])
    _force_step(app, "shallow_check")
    assert any("has_id" in v for v in app.state["shallow_violations"])


def test_shallow_catches_empty_batch():
    app = build_application()
    _force_step(app, "ingest", batch=[])
    _force_step(app, "shallow_check")
    assert any("non_empty" in v for v in app.state["shallow_violations"])


@pytest.mark.asyncio
async def test_class_action_works_through_mcp_step():
    """The class-based actions surface as regular MCP step targets;
    the agent calls them by the name registered in with_actions."""
    server = mount(
        build_application,
        mode=ServingMode.STEP,
        name="class-action-test",
    )
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "ingest",
                "inputs": {"batch": [{"id": 1, "value": 10}]},
            },
        )
        r = await client.call_tool("step", {"action": "shallow_check", "inputs": {}})
        out = r.structured_content
        assert out.get("error") is None
        assert out["state"]["shallow_violations"] == []
        r = await client.call_tool("step", {"action": "deep_check", "inputs": {}})
        out = r.structured_content
        assert out.get("error") is None
        r = await client.call_tool("step", {"action": "finalize", "inputs": {}})
        out = r.structured_content
        assert out["state"]["verdict"] == "clean"
