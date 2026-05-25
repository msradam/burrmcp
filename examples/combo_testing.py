"""Combinatorial software testing: hunt 2-way interaction bugs.

Companion demo to ``combinatoric_testing.py``. Same architectural
pattern (Hamilton DAG + Burr FSM + Theodosia), different SUT shape:
where ``combinatoric_testing`` sweeps numeric parameters looking for
algorithmic divergence, this one sweeps categorical parameters looking
for interaction bugs in a piece of business logic.

The SUT is a checkout pricing engine with four categorical inputs
(tier, region, payment, coupon) plus quantity and base_price. The
"production" implementation has three seeded 2-way interaction bugs:

* ``tier=enterprise & coupon=loyalty`` double-applies the tier
  discount.
* ``region=APAC & coupon=seasonal`` skips FX conversion.
* ``region=EU & payment=wire`` flips the wire-transfer fee sign.

Most single-dimension changes go through correct code. The bug surface
is only in the 2-way interactions. This is exactly the shape
pairwise-coverage tools (PICT, ACTS, allpairs) are designed for: with
3^4 = 81 base combos but bugs only in three 2-way interactions, an LLM
that hypothesises about which dimensions matter and varies one at a
time should localise the bugs in well under 81 trials.

FSM shape:

    initialize -> run_test -> run_test -> ... -> finalize

Each ``run_test`` call runs both implementations through the Hamilton
DAG and records whether they agreed. ``finalize`` summarises the
search and tallies failures per (dimension, value) so the caller LLM
can see which dimensions drove the bugs.

Run:

    uv run python examples/combo_testing.py

A typical session:

    initialize()
    run_test(tier="free", region="US", payment="card", coupon="none")
    run_test(tier="enterprise", region="US", payment="card", coupon="loyalty")  # bug 1
    run_test(tier="premium", region="APAC", payment="card", coupon="seasonal")  # bug 2
    run_test(tier="premium", region="EU", payment="wire", coupon="none")        # bug 3
    finalize()
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "combo-testing-demo"
_DAG_PATH = Path(__file__).parent / "data" / "combo_testing" / "dag.py"
_DAG_MODULE_NAME = "combo_testing_dag"

_dag_lock = threading.Lock()
_dag_module: Any = None


def _load_dag_module() -> Any:
    """Load the Hamilton DAG module and cache it.

    Same pattern as the other Hamilton-using demos. The DAG module
    augments sys.path on import so its sibling ``checkout.py`` module
    is importable as a top-level name regardless of which entrypoint
    loads the DAG.
    """
    global _dag_module
    with _dag_lock:
        if _dag_module is not None:
            return _dag_module
        spec = importlib.util.spec_from_file_location(_DAG_MODULE_NAME, _DAG_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load DAG module at {_DAG_PATH}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_DAG_MODULE_NAME] = mod
        spec.loader.exec_module(mod)
        _dag_module = mod
        return mod


_DAG_FINAL_VARS = ("production_total", "reference_total", "verdict")


def _run_dag(
    tier: str,
    region: str,
    payment: str,
    coupon: str,
    quantity: int,
    base_price: float,
) -> dict[str, Any]:
    """Execute the Hamilton DAG for one combo."""
    from hamilton import driver

    mod = _load_dag_module()
    dr = driver.Builder().with_modules(mod).build()
    return dr.execute(
        list(_DAG_FINAL_VARS),
        inputs={
            "tier_input": tier,
            "region_input": region,
            "payment_input": payment,
            "coupon_input": coupon,
            "quantity_input": quantity,
            "base_price_input": base_price,
        },
    )


# == FSM actions =====================================================

# Literal types so FastMCP serialises the param surface as JSON
# Schema enums; an LLM connecting to the server sees the discrete
# choices and stays inside the valid space.
_Tier = Literal["free", "premium", "enterprise"]
_Region = Literal["US", "EU", "APAC"]
_Payment = Literal["card", "wire", "crypto"]
_Coupon = Literal["none", "seasonal", "loyalty"]


@action(reads=[], writes=["history", "status"])
def initialize(state: State) -> State:
    """Open the combo-testing session."""
    return state.update(history=[], status="testing")


@action(reads=["history"], writes=["history", "last_trial", "status"])
def run_test(
    state: State,
    tier: _Tier,
    region: _Region,
    payment: _Payment,
    coupon: _Coupon,
    quantity: int = 1,
    base_price: float = 100.0,
) -> State:
    """Run one (tier, region, payment, coupon) combo through both SUTs.

    Both production and reference implementations execute via the
    Hamilton DAG with the same inputs. The assertion node folds the
    comparison into ``matches`` (bool) and ``delta`` (production minus
    reference, in dollars). Use the running history to spot patterns:
    which dimensions tend to show up in failing combos.

    Args:
        tier: customer tier (free / premium / enterprise)
        region: customer region (US / EU / APAC)
        payment: payment method (card / wire / crypto)
        coupon: coupon applied (none / seasonal / loyalty)
        quantity: items in the order, defaults to 1
        base_price: per-item base price, defaults to 100.0
    """
    out = _run_dag(tier, region, payment, coupon, int(quantity), float(base_price))
    entry = {
        "trial": len(state["history"]) + 1,
        "inputs": {
            "tier": tier,
            "region": region,
            "payment": payment,
            "coupon": coupon,
            "quantity": int(quantity),
            "base_price": float(base_price),
        },
        "production": out["production_total"],
        "reference": out["reference_total"],
        "delta": out["verdict"]["delta"],
        "matches": out["verdict"]["matches"],
    }
    return state.update(
        history=[*state["history"], entry],
        last_trial=entry,
        status="testing",
    )


@action(reads=["history"], writes=["summary", "status"])
def finalize(state: State) -> State:
    """Summarise the search.

    Reports the failure list and a per-(dimension, value) tally so the
    caller LLM can see which dimensions drove the bugs. For pairwise
    interaction bugs this tally is what surfaces the structure: a
    dimension that appears in many failures is implicated, and pairs of
    dimensions that always co-occur in failures point at the precise
    2-way interaction.
    """
    history = state["history"]
    failures = [e for e in history if not e["matches"]]
    by_dimension: dict[str, dict[str, int]] = {}
    for dim in ("tier", "region", "payment", "coupon"):
        per_value: dict[str, int] = {}
        for entry in failures:
            v = entry["inputs"][dim]
            per_value[v] = per_value.get(v, 0) + 1
        by_dimension[dim] = per_value
    return state.update(
        summary={
            "trials": len(history),
            "failures": len(failures),
            "failure_rate": (len(failures) / len(history)) if history else 0.0,
            "failing_combos": [{"inputs": e["inputs"], "delta": e["delta"]} for e in failures[:20]],
            "failures_by_dimension": by_dimension,
        },
        status="terminated",
    )


# == graph ===========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            initialize=initialize,
            run_test=run_test,
            finalize=finalize,
        )
        .with_transitions(
            ("initialize", "run_test"),
            # Both edges share a condition so Burr accepts them as
            # parallel valid_next moves; the caller LLM picks via
            # step(action=...).
            ("run_test", "run_test", Condition.expr("status == 'testing'")),
            ("run_test", "finalize", Condition.expr("status == 'testing'")),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            history=[],
            last_trial=None,
            summary=None,
            status="initial",
        )
        .with_entrypoint("initialize")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="combo-testing",
        instructions=(
            "Categorical combinatorial testing FSM. The SUT is a "
            "checkout pricing engine with 4 categorical inputs "
            "(tier in {free, premium, enterprise}, region in "
            "{US, EU, APAC}, payment in {card, wire, crypto}, "
            "coupon in {none, seasonal, loyalty}) plus quantity and "
            "base_price. The production implementation has three "
            "seeded 2-way interaction bugs; the reference is correct. "
            "Goal: find combos where production disagrees with "
            "reference. Loop: call run_test(tier=..., region=..., "
            "payment=..., coupon=...) repeatedly. Each call returns "
            "the dollar delta between the two and whether they match. "
            "Use the running history to hypothesise about which "
            "2-way interactions are buggy: if changing one dimension "
            "flips matches True to False while others stay constant, "
            "that dimension is implicated. Call finalize when "
            "satisfied; the summary tallies failures per (dimension, "
            "value) and lists failing combos for inspection."
        ),
    )


if __name__ == "__main__":
    build_server().run()
