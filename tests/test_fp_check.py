"""Tests for examples/fp_check.py.

Same shape as test_differential_review.py:

* Action-level unit tests for validation (empty bug_summary,
  can_restate_clearly=False, missing keys, invalid path, invalid
  verdict, incomplete gate set).
* FSM-level walks via _force_step for both TRUE POSITIVE and FALSE
  POSITIVE paths.
* MCP wire roundtrip confirming the FSM refuses skipping Step 0 and
  refuses calling final_verdict before all six gates have fired.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from fp_check import (
    build_application,
    build_server,
    final_verdict,
    gate1_process,
    gate2_reachability,
    route_path,
    start_check,
    step0_restate,
)


def _initial_state(**overrides):
    from burr.core.state import State

    base = {
        "bug_summary": "",
        "restated": {},
        "path": "",
        "path_justification": "",
        "gate_results": {},
        "verdict": None,
        "verdict_summary": None,
        "current_prompt": "",
        "log": [],
    }
    base.update(overrides)
    return State(base)


# == action unit tests ==========================================


def test_start_check_rejects_empty_bug_summary():
    with pytest.raises(ValueError):
        start_check(_initial_state(), bug_summary="   ")


def test_step0_refuses_when_can_restate_clearly_is_false():
    s = _initial_state(log=["..."])
    restated = {
        "exact_claim": "x",
        "alleged_root_cause": "y",
        "supposed_trigger": "z",
        "claimed_impact": "w",
        "threat_model": "u",
        "bug_class": "logic",
        "can_restate_clearly": False,
    }
    with pytest.raises(ValueError, match="can_restate_clearly"):
        step0_restate(s, restated=restated)


def test_step0_refuses_missing_keys():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError, match="missing required"):
        step0_restate(s, restated={"exact_claim": "x"})


def test_route_path_rejects_unknown_path():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError):
        route_path(s, path="medium", justification="")


def test_gate_rejects_unknown_verdict():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError, match="verdict"):
        gate1_process(s, verdict="maybe", evidence={})


def test_gate_records_into_gate_results_dict():
    s = _initial_state(log=["..."])
    out = gate2_reachability(s, verdict="pass", evidence={"path": "X"})
    assert out["gate_results"]["reachability"]["verdict"] == "pass"
    assert out["gate_results"]["reachability"]["evidence"] == {"path": "X"}


def test_final_verdict_refuses_when_gates_missing():
    s = _initial_state(gate_results={"process": {"verdict": "pass", "evidence": {}}})
    with pytest.raises(ValueError, match="have no recorded outcome"):
        final_verdict(s)


def test_final_verdict_returns_true_positive_when_all_pass():
    gate_results = {
        name: {"verdict": "pass", "evidence": {}}
        for name in ("process", "reachability", "impact", "poc", "math", "environment")
    }
    s = _initial_state(gate_results=gate_results, log=["..."])
    out = final_verdict(s, notes="all clear")
    assert out["verdict"] == "TRUE POSITIVE"
    assert out["verdict_summary"]["failed_gates"] == []
    assert out["verdict_summary"]["load_bearing_gate"] is None


def test_final_verdict_returns_false_positive_when_any_gate_fails():
    gate_results = {
        "process": {"verdict": "pass", "evidence": {}},
        "reachability": {"verdict": "pass", "evidence": {}},
        "impact": {"verdict": "pass", "evidence": {}},
        "poc": {"verdict": "fail", "evidence": {"why": "no PoC"}},
        "math": {"verdict": "fail", "evidence": {"why": "validation prevents"}},
        "environment": {"verdict": "pass", "evidence": {}},
    }
    s = _initial_state(gate_results=gate_results, log=["..."])
    out = final_verdict(s, notes="...")
    assert out["verdict"] == "FALSE POSITIVE"
    assert set(out["verdict_summary"]["failed_gates"]) == {"poc", "math"}
    # load-bearing gate is the FIRST failing gate by SKILL convention.
    assert out["verdict_summary"]["load_bearing_gate"] == "poc"


# == FSM walks ==================================================


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def _full_restated(**overrides):
    base = {
        "exact_claim": "x",
        "alleged_root_cause": "y",
        "supposed_trigger": "z",
        "claimed_impact": "w",
        "threat_model": "unauth remote",
        "bug_class": "memory_corruption",
        "can_restate_clearly": True,
    }
    base.update(overrides)
    return base


def test_full_walk_true_positive():
    app = build_application()
    _force_step(app, "start_check", bug_summary="heap overflow in parse_header")
    _force_step(app, "step0_restate", restated=_full_restated())
    _force_step(app, "route_path", path="standard")
    for gate in (
        "gate1_process",
        "gate2_reachability",
        "gate3_impact",
        "gate4_poc_validation",
        "gate5_math_bounds",
        "gate6_environment",
    ):
        _force_step(app, gate, verdict="pass", evidence={"note": "ok"})
    _force_step(app, "final_verdict", notes="confirmed")
    assert app.state["verdict"] == "TRUE POSITIVE"


def test_full_walk_false_positive_when_one_gate_fails():
    app = build_application()
    _force_step(app, "start_check", bug_summary="integer underflow")
    _force_step(app, "step0_restate", restated=_full_restated(bug_class="integer"))
    _force_step(app, "route_path", path="standard")
    _force_step(app, "gate1_process", verdict="pass", evidence={})
    _force_step(app, "gate2_reachability", verdict="pass", evidence={})
    _force_step(app, "gate3_impact", verdict="pass", evidence={})
    _force_step(app, "gate4_poc_validation", verdict="pass", evidence={})
    _force_step(app, "gate5_math_bounds", verdict="fail", evidence={"why": "validation prevents"})
    _force_step(app, "gate6_environment", verdict="pass", evidence={})
    _force_step(app, "final_verdict", notes="upstream check makes condition impossible")
    assert app.state["verdict"] == "FALSE POSITIVE"
    assert app.state["verdict_summary"]["load_bearing_gate"] == "math"


# == MCP wire roundtrip ========================================


@pytest.mark.asyncio
async def test_mcp_refuses_skipping_step0():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "start_check", "inputs": {"bug_summary": "buffer overflow somewhere"}},
        )
        # Try to skip Step 0 and go straight to route_path: refused.
        r = await client.call_tool(
            "step",
            {"action": "route_path", "inputs": {"path": "standard"}},
        )
        out = r.structured_content
        assert out.get("error") == "invalid_transition"
        assert "step0_restate" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_mcp_refuses_final_verdict_before_all_gates():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "start_check", "inputs": {"bug_summary": "x"}},
        )
        await client.call_tool(
            "step",
            {"action": "step0_restate", "inputs": {"restated": _full_restated()}},
        )
        await client.call_tool(
            "step",
            {"action": "route_path", "inputs": {"path": "standard"}},
        )
        # Try to call final_verdict before any gate fires: refused.
        r = await client.call_tool(
            "step",
            {"action": "final_verdict", "inputs": {"notes": "skip"}},
        )
        out = r.structured_content
        assert out.get("error") == "invalid_transition"
        assert "gate1_process" in out["valid_next_actions"]
