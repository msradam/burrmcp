"""Tests for examples/combo_testing.py.

Three layers, mirroring test_combinatoric_testing.py:

* SUT unit tests: the reference implementation is bug-free; the
  production implementation matches it on baseline combos and
  diverges on each of the three seeded 2-way interactions.
* Hamilton DAG execution: drive the DAG directly to confirm the
  verdict node folds the comparison.
* FSM-level tests: walk initialize -> run_test x N -> finalize via
  Burr's step() machinery using the _force_step helper, and via
  mount() + Client over the MCP wire format.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))
sys.path.insert(0, str(_REPO_ROOT / "examples" / "data" / "combo_testing"))

from checkout import process_order, reference_process_order
from combo_testing import (
    build_application,
    build_server,
    finalize,
    initialize,
    run_test,
)

# == SUT unit tests ================================================


def test_reference_and_production_agree_on_baseline():
    """No seeded interaction triggered: both implementations match."""
    args = ("free", "US", "card", "none", 1, 100.0)
    assert process_order(*args) == reference_process_order(*args)


def test_bug1_enterprise_plus_loyalty_diverges():
    """Tier discount stacks twice in production; reference does not."""
    args = ("enterprise", "US", "card", "loyalty", 1, 100.0)
    prod = process_order(*args)
    ref = reference_process_order(*args)
    assert prod != ref
    # Production should be lower than reference because the double
    # discount makes the order cheaper than it should be.
    assert prod < ref


def test_bug2_apac_plus_seasonal_diverges():
    """FX conversion is skipped in production; reference applies 0.95."""
    args = ("premium", "APAC", "card", "seasonal", 1, 100.0)
    prod = process_order(*args)
    ref = reference_process_order(*args)
    assert prod != ref
    # Skipping the 0.95 multiplier means production charges more.
    assert prod > ref


def test_bug3_eu_plus_wire_diverges():
    """Wire fee sign is flipped in production for EU."""
    args = ("premium", "EU", "wire", "none", 1, 100.0)
    prod = process_order(*args)
    ref = reference_process_order(*args)
    assert prod != ref
    # A flipped (negative) fee makes production undercharge.
    assert prod < ref


def test_single_dimension_changes_do_not_trigger_bugs():
    """Changing one dimension while holding others on the baseline
    must not surface any of the seeded 2-way bugs."""
    for tier in ("free", "premium", "enterprise"):
        args = (tier, "US", "card", "none", 1, 100.0)
        assert process_order(*args) == reference_process_order(*args), (
            f"single-dim bug at tier={tier}"
        )
    for region in ("US", "EU", "APAC"):
        args = ("free", region, "card", "none", 1, 100.0)
        assert process_order(*args) == reference_process_order(*args), (
            f"single-dim bug at region={region}"
        )


def test_quantity_and_base_price_scale_through_both_implementations():
    """Non-categorical inputs (quantity, base_price) should affect both
    sides identically; bugs are only in categorical interactions."""
    a = ("free", "US", "card", "none", 5, 250.0)
    b = ("free", "US", "card", "none", 5, 250.0)
    assert process_order(*a) == reference_process_order(*b)


# == Hamilton DAG execution =======================================


def test_dag_folds_verdict_for_matching_combo():
    from combo_testing import _load_dag_module
    from hamilton import driver

    dr = driver.Builder().with_modules(_load_dag_module()).build()
    out = dr.execute(
        ["production_total", "reference_total", "verdict"],
        inputs={
            "tier_input": "free",
            "region_input": "US",
            "payment_input": "card",
            "coupon_input": "none",
            "quantity_input": 1,
            "base_price_input": 100.0,
        },
    )
    assert out["verdict"]["matches"] is True
    assert out["verdict"]["delta"] == 0.0


def test_dag_folds_verdict_for_buggy_combo():
    from combo_testing import _load_dag_module
    from hamilton import driver

    dr = driver.Builder().with_modules(_load_dag_module()).build()
    out = dr.execute(
        ["production_total", "reference_total", "verdict"],
        inputs={
            "tier_input": "enterprise",
            "region_input": "US",
            "payment_input": "card",
            "coupon_input": "loyalty",
            "quantity_input": 1,
            "base_price_input": 100.0,
        },
    )
    assert out["verdict"]["matches"] is False
    assert out["verdict"]["delta"] != 0


# == FSM action unit tests ========================================


def test_initialize_resets_history():
    from burr.core.state import State

    s = State({"history": ["old"], "status": "initial"})
    out = initialize(s)
    assert out["history"] == []
    assert out["status"] == "testing"


def test_run_test_records_matching_trial():
    from burr.core.state import State

    s = State({"history": [], "status": "testing", "last_trial": None})
    out = run_test(s, tier="free", region="US", payment="card", coupon="none")
    assert len(out["history"]) == 1
    trial = out["history"][0]
    assert trial["matches"] is True
    assert trial["delta"] == 0.0
    assert trial["inputs"]["tier"] == "free"
    assert out["last_trial"] == trial


def test_run_test_records_buggy_trial():
    from burr.core.state import State

    s = State({"history": [], "status": "testing", "last_trial": None})
    out = run_test(
        s,
        tier="enterprise",
        region="US",
        payment="card",
        coupon="loyalty",
    )
    trial = out["history"][0]
    assert trial["matches"] is False
    assert trial["delta"] != 0


def test_finalize_tallies_failures_by_dimension():
    from burr.core.state import State

    history = [
        {
            "trial": 1,
            "inputs": {
                "tier": "enterprise",
                "region": "US",
                "payment": "card",
                "coupon": "loyalty",
            },
            "matches": False,
            "delta": -15.0,
        },
        {
            "trial": 2,
            "inputs": {
                "tier": "enterprise",
                "region": "EU",
                "payment": "card",
                "coupon": "loyalty",
            },
            "matches": False,
            "delta": -16.0,
        },
        {
            "trial": 3,
            "inputs": {
                "tier": "free",
                "region": "US",
                "payment": "card",
                "coupon": "none",
            },
            "matches": True,
            "delta": 0.0,
        },
    ]
    s = State({"history": history, "status": "testing", "summary": None})
    out = finalize(s)
    summary = out["summary"]
    assert summary["trials"] == 3
    assert summary["failures"] == 2
    # The shared dimensions across both failures are tier=enterprise
    # and coupon=loyalty; the by-dimension tally should reflect that.
    assert summary["failures_by_dimension"]["tier"]["enterprise"] == 2
    assert summary["failures_by_dimension"]["coupon"]["loyalty"] == 2
    assert "free" not in summary["failures_by_dimension"]["tier"]


def test_finalize_handles_empty_history():
    from burr.core.state import State

    s = State({"history": [], "status": "testing", "summary": None})
    out = finalize(s)
    assert out["summary"]["trials"] == 0
    assert out["summary"]["failures"] == 0
    assert out["summary"]["failure_rate"] == 0.0


# == FSM-level walk + MCP roundtrip ===============================


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_full_walk_finds_all_three_seeded_bugs():
    app = build_application()
    _force_step(app, "initialize")
    targeted = [
        ("enterprise", "US", "card", "loyalty"),
        ("premium", "APAC", "card", "seasonal"),
        ("premium", "EU", "wire", "none"),
        ("free", "US", "card", "none"),
    ]
    for tier, region, payment, coupon in targeted:
        _force_step(
            app,
            "run_test",
            tier=tier,
            region=region,
            payment=payment,
            coupon=coupon,
        )
    _force_step(app, "finalize")
    summary = app.state["summary"]
    assert summary["failures"] == 3
    assert summary["trials"] == 4


@pytest.mark.asyncio
async def test_mcp_step_through_combo_search():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "initialize"})
        out = json.loads(r.content[0].text)
        assert out["action"] == "initialize"

        r = await client.call_tool(
            "step",
            {
                "action": "run_test",
                "inputs": {
                    "tier": "enterprise",
                    "region": "US",
                    "payment": "card",
                    "coupon": "loyalty",
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert out["action"] == "run_test"
        assert out["state"]["last_trial"]["matches"] is False
        assert out["state"]["last_trial"]["delta"] != 0

        r = await client.call_tool("step", {"action": "finalize"})
        out = json.loads(r.content[0].text)
        assert out["state"]["summary"]["failures"] == 1
