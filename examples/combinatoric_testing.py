"""Combinatoric differential testing: Hamilton + Burr + BurrMCP together.

The pitch: each library does what it is best at. Hamilton declares the
test DAG (param nodes, derivation nodes, assertion nodes). Burr wraps
the search loop (``propose_and_run -> propose_and_run -> finalize``)
with gated transitions and a session-tracked audit trail. BurrMCP
turns the loop into a tool the caller LLM drives, slot-filling
parameter values on each call.

The system under test (SUT) here is two percentile implementations:

* ``percentile_linear``: linear interpolation between adjacent ranks
  (``numpy.percentile(method="linear")``-style).
* ``percentile_nearest``: nearest-rank method (Wikipedia's standard
  definition).

Both agree on the median for sorted lists. They diverge at non-median
percentiles whenever the position lands between two ranks. The caller
LLM's job is to search the input space for combos where they disagree
most: vary the values list shape, sweep different percentiles, build
hypotheses from the abs_diff values returned by earlier trials, and
call ``finalize`` when satisfied with the best divergence found.

This demo is what BurrMCP enables that's hard to express otherwise.
LLM-driven adaptive parameter search where every iteration is a
fully tracked Burr session, every divergence is reproducible via
``fork_from_past``, and the SUT model lives as a Hamilton DAG that
can be extended one node at a time without changing the FSM.

Run:

    uv run python examples/combinatoric_testing.py

A typical session:

    initialize(task="find inputs where the two percentile methods disagree most")
    propose_and_run(values=[1, 2, 3, 4, 5], p=50)  # both return 3.0; abs_diff=0
    propose_and_run(values=[1, 2, 3, 4, 5], p=80)  # linear=4.2, nearest=4; abs_diff=0.2
    propose_and_run(values=[1, 2, 100], p=66)      # diverges much more
    propose_and_run(values=[1, 1000, 1000, 1000], p=25)  # hunt extremes
    finalize()
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "combinatoric-testing-demo"
_DAG_PATH = Path(__file__).parent / "data" / "combinatoric_testing" / "dag.py"
_DAG_MODULE_NAME = "combinatoric_testing_dag"

_dag_lock = threading.Lock()
_dag_module: Any = None


def _load_dag_module() -> Any:
    """Load the Hamilton DAG module and cache it.

    Registered in ``sys.modules`` so Hamilton's graph walker resolves
    upstream nodes by module name. The DAG module imports the SUT
    functions at top level; the cached module is what every call to
    the FSM uses.
    """
    global _dag_module
    with _dag_lock:
        if _dag_module is not None:
            return _dag_module
        spec = importlib.util.spec_from_file_location(_DAG_MODULE_NAME, _DAG_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load Hamilton DAG module at {_DAG_PATH}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_DAG_MODULE_NAME] = mod
        spec.loader.exec_module(mod)
        _dag_module = mod
        return mod


def _run_dag(values_input: list[float], p_input: float) -> dict[str, Any]:
    """Build a Hamilton driver, execute the differential-test DAG.

    Lazy-imports ``hamilton`` so the FSM module is importable even
    without Hamilton installed. ``examples/burrmcp serve`` callers
    that just want to mount the FSM don't need the dep; only running
    the actions does.
    """
    from hamilton import driver

    mod = _load_dag_module()
    dr = driver.Builder().with_modules(mod).build()
    return dr.execute(
        ["v1_result", "v2_result", "divergence"],
        inputs={"values_input": values_input, "p_input": p_input},
    )


# == FSM actions =====================================================


@action(reads=[], writes=["history", "status", "task"])
def initialize(
    state: State,
    task: str = ("find inputs where percentile_linear and percentile_nearest disagree most"),
) -> State:
    """Open the search session.

    Accepts an optional ``task`` string the caller can use to record
    its own goal; it's echoed back in subsequent state reads so an
    observer reading ``burr://state`` mid-session can tell what the
    LLM was searching for.
    """
    return state.update(history=[], status="initialized", task=task)


@action(reads=["history"], writes=["history", "last_trial", "status"])
def propose_and_run(
    state: State,
    values: list[float],
    p: float,
) -> State:
    """Propose a parameter combo and run the Hamilton DAG.

    Both SUT implementations execute with the same inputs through the
    DAG; the assertion node folds the comparison into an ``abs_diff``
    and a ``diverges`` flag. The trial is appended to ``history`` so
    later actions can summarise the search, and ``last_trial`` exposes
    the freshest result for the caller LLM to read off and steer
    its next pick.

    Inputs:
        values: List of floats. Must be non-empty.
        p: Percentile in [0, 100].
    """
    if not values:
        raise ValueError("values must be non-empty")
    p_float = float(p)
    values_float = [float(v) for v in values]
    if not 0 <= p_float <= 100:
        raise ValueError(f"p must be in [0, 100], got {p_float}")

    out = _run_dag(values_float, p_float)
    entry = {
        "trial": len(state["history"]) + 1,
        "values": values_float,
        "p": p_float,
        "v1": out["v1_result"],
        "v2": out["v2_result"],
        "abs_diff": out["divergence"]["abs_diff"],
        "diverges": out["divergence"]["diverges"],
    }
    return state.update(
        history=[*state["history"], entry],
        last_trial=entry,
        status="searching",
    )


@action(reads=["history"], writes=["summary", "status"])
def finalize(state: State) -> State:
    """Summarise the search and exit.

    Reports the trial with the largest absolute divergence found. With
    zero trials, returns a degenerate summary so the FSM still ends
    cleanly. Terminal.
    """
    history = state["history"]
    if not history:
        return state.update(
            summary={
                "trials": 0,
                "max_abs_diff": 0.0,
                "diverging_count": 0,
                "best": None,
            },
            status="terminated",
        )
    sorted_by_diff = sorted(history, key=lambda e: e["abs_diff"], reverse=True)
    return state.update(
        summary={
            "trials": len(history),
            "max_abs_diff": sorted_by_diff[0]["abs_diff"],
            "diverging_count": sum(1 for e in history if e["diverges"]),
            "best": sorted_by_diff[0],
        },
        status="terminated",
    )


# == graph ===========================================================


def build_application(task: str | None = None):
    """Construct the search Application."""
    return (
        ApplicationBuilder()
        .with_actions(
            initialize=initialize,
            propose_and_run=propose_and_run,
            finalize=finalize,
        )
        .with_transitions(
            ("initialize", "propose_and_run"),
            # Both edges share a condition so Burr accepts both as valid
            # next actions while the search is open. The caller LLM
            # picks via step(action=...); without the explicit condition
            # Burr would refuse the second as a "redundant default".
            ("propose_and_run", "propose_and_run", Condition.expr("status == 'searching'")),
            ("propose_and_run", "finalize", Condition.expr("status == 'searching'")),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            history=[],
            last_trial=None,
            summary=None,
            status="initial",
            task=(
                task or "find inputs where percentile_linear and percentile_nearest disagree most"
            ),
        )
        .with_entrypoint("initialize")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="combinatoric-testing",
        instructions=(
            "Differential percentile testing FSM. Two implementations "
            "(percentile_linear with interpolation; percentile_nearest "
            "via nearest-rank) are compared on every trial. Goal: find "
            "inputs where they diverge most. Loop: call "
            "propose_and_run(values=[...], p=...) repeatedly; each "
            "call returns abs_diff (absolute difference between the "
            "two implementations) and appends the trial to history. "
            "Use prior abs_diff values to guide the next pick. Larger "
            "abs_diff is better. Call finalize when satisfied; the "
            "summary reports the best (most-divergent) input found. "
            "fork_from_past lets you resume a search across server "
            "restarts; fork_at lets you rewind to a promising trial "
            "and vary one related parameter without rerunning the "
            "whole search."
        ),
    )


if __name__ == "__main__":
    build_server().run()
