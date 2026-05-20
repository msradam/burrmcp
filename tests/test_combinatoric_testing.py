"""Tests for examples/combinatoric_testing.py.

Three layers:

* SUT unit tests: each percentile implementation in isolation. Confirms
  the two SUTs agree at the median for sorted input and diverge at
  non-median percentiles when the position falls between ranks.
* DAG execution test: drive the Hamilton DAG directly, verifying the
  same inputs flow through both SUTs and the divergence node folds
  them correctly.
* FSM-level tests: walk initialize -> propose_and_run x N -> finalize
  through Burr's step() machinery using the _force_step helper
  (same pattern as test_sqlite_persister.py), and through mount() +
  Client for the MCP wire format.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

# Make the demo module + its data dir importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))
sys.path.insert(0, str(_REPO_ROOT / "examples" / "data" / "combinatoric_testing"))

from combinatoric_testing import (  # noqa: E402
    build_application,
    build_server,
    finalize,
    initialize,
    propose_and_run,
)
from sut import percentile_linear, percentile_nearest  # noqa: E402

# == SUT unit tests ================================================


def test_sut_agree_at_median_for_odd_sorted_list():
    values = [1, 2, 3, 4, 5]
    assert percentile_linear(values, 50) == 3.0
    assert percentile_nearest(values, 50) == 3.0


def test_sut_diverge_at_non_median():
    values = [1, 2, 3, 4, 5]
    # 80th percentile by linear interp: rank=3.2, between values[3]=4
    # and values[4]=5, interpolated to 4.2.
    # 80th percentile by nearest rank: ceil(0.8*5)=4 (1-indexed),
    # returns values[3]=4.
    assert percentile_linear(values, 80) == pytest.approx(4.2)
    assert percentile_nearest(values, 80) == 4.0
    # Confirm they actually disagree.
    assert percentile_linear(values, 80) != percentile_nearest(values, 80)


def test_sut_reject_empty_values():
    with pytest.raises(ValueError):
        percentile_linear([], 50)
    with pytest.raises(ValueError):
        percentile_nearest([], 50)


def test_sut_reject_out_of_range_p():
    with pytest.raises(ValueError):
        percentile_linear([1, 2, 3], -1)
    with pytest.raises(ValueError):
        percentile_nearest([1, 2, 3], 101)


def test_sut_handles_single_element():
    assert percentile_linear([42], 50) == 42.0
    assert percentile_nearest([42], 50) == 42.0


# == Hamilton DAG execution =======================================


def test_dag_runs_both_suts_and_folds_into_divergence():
    """Drive the DAG directly; structure of returned dict is the
    contract the FSM action depends on."""
    import dag as dag_module
    from hamilton import driver

    dr = driver.Builder().with_modules(dag_module).build()
    out = dr.execute(
        ["v1_result", "v2_result", "divergence"],
        inputs={"values_input": [1.0, 2.0, 3.0, 4.0, 5.0], "p_input": 80.0},
    )
    assert out["v1_result"] == pytest.approx(4.2)
    assert out["v2_result"] == 4.0
    assert out["divergence"]["abs_diff"] == pytest.approx(0.2)
    assert out["divergence"]["diverges"] is True


# == FSM action unit tests ========================================


def test_initialize_action_resets_history_and_records_task():
    """The initialize action seeds history=[] and stamps the task
    string so downstream actions can read it. Works on a bare State."""
    from burr.core.state import State

    s = State({"history": ["old"], "status": "initial", "task": ""})
    out = initialize(s, task="hunt divergence")
    assert out["history"] == []
    assert out["status"] == "initialized"
    assert out["task"] == "hunt divergence"


def test_propose_and_run_appends_trial():
    from burr.core.state import State

    s = State({"history": [], "status": "initialized", "last_trial": None})
    out = propose_and_run(s, values=[1, 2, 3, 4, 5], p=80)
    assert len(out["history"]) == 1
    trial = out["history"][0]
    assert trial["trial"] == 1
    assert trial["p"] == 80.0
    assert trial["v1"] == pytest.approx(4.2)
    assert trial["v2"] == 4.0
    assert trial["abs_diff"] == pytest.approx(0.2)
    assert trial["diverges"] is True
    assert out["last_trial"] == trial


def test_propose_and_run_rejects_empty_values():
    from burr.core.state import State

    s = State({"history": [], "status": "initialized", "last_trial": None})
    with pytest.raises(ValueError):
        propose_and_run(s, values=[], p=50)


def test_finalize_picks_best_by_abs_diff():
    from burr.core.state import State

    history = [
        {
            "trial": 1,
            "values": [1, 2, 3],
            "p": 50,
            "v1": 2.0,
            "v2": 2.0,
            "abs_diff": 0.0,
            "diverges": False,
        },
        {
            "trial": 2,
            "values": [1, 2, 3],
            "p": 80,
            "v1": 2.6,
            "v2": 3.0,
            "abs_diff": 0.4,
            "diverges": True,
        },
        {
            "trial": 3,
            "values": [1, 100],
            "p": 25,
            "v1": 25.75,
            "v2": 1.0,
            "abs_diff": 24.75,
            "diverges": True,
        },
    ]
    s = State({"history": history, "status": "searching", "summary": None})
    out = finalize(s)
    assert out["summary"]["trials"] == 3
    assert out["summary"]["diverging_count"] == 2
    assert out["summary"]["max_abs_diff"] == pytest.approx(24.75)
    assert out["summary"]["best"]["trial"] == 3
    assert out["status"] == "terminated"


def test_finalize_handles_empty_history():
    from burr.core.state import State

    s = State({"history": [], "status": "initialized", "summary": None})
    out = finalize(s)
    assert out["summary"]["trials"] == 0
    assert out["summary"]["best"] is None


# == FSM-level walk via _force_step helper ========================


def _force_step(app, action_name: str, **inputs):
    """Walk one named step regardless of transition order.

    Same pattern as tests/test_sqlite_persister.py::_force_step. Burr's
    auto-routing picks the first matching transition; the MCP adapter
    handles agent-chosen actions by overriding get_next_action, and
    tests that walk outside MCP do the same.
    """
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_full_walk_produces_summary_with_best_trial():
    app = build_application()
    _force_step(app, "initialize")
    _force_step(app, "propose_and_run", values=[1, 2, 3, 4, 5], p=50)
    _force_step(app, "propose_and_run", values=[1, 2, 3, 4, 5], p=80)
    _force_step(app, "propose_and_run", values=[1, 1, 1000], p=66)
    _force_step(app, "finalize")

    summary = app.state["summary"]
    assert summary is not None
    assert summary["trials"] == 3
    assert summary["best"]["abs_diff"] > 0


# == mount() + MCP wire roundtrip =================================


@pytest.mark.asyncio
async def test_mcp_step_through_full_search():
    """Drive the FSM through the MCP step tool end-to-end."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "initialize"})
        out = json.loads(r.content[0].text)
        assert out["action"] == "initialize"

        r = await client.call_tool(
            "step",
            {"action": "propose_and_run", "inputs": {"values": [1, 2, 3, 4, 5], "p": 80}},
        )
        out = json.loads(r.content[0].text)
        assert out["action"] == "propose_and_run"
        assert out["state"]["last_trial"]["abs_diff"] == pytest.approx(0.2)

        r = await client.call_tool("step", {"action": "finalize"})
        out = json.loads(r.content[0].text)
        assert out["action"] == "finalize"
        assert out["state"]["summary"]["trials"] == 1
        assert out["state"]["summary"]["best"]["abs_diff"] == pytest.approx(0.2)
