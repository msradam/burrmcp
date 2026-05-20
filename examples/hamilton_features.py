"""Burr-plus-Hamilton: FSM for workflow, DAG for dataflow.

This example pairs Burr (DAGWorks' state machine library) with
Hamilton (DAGWorks' dataflow library) inside one MCP server. The
split is the canonical DAGWorks story: Burr handles the workflow
(gated transitions, audit, retry surface, terminal states), Hamilton
handles the dataflow inside a single step (a column DAG with no
control flow). One Burr action body just builds a Hamilton driver
and calls ``execute(...)``; the FSM around it adds the gating and
the visible step boundaries that a pure Hamilton run does not have.

Domain: loan-application risk scoring.

    submit_application -> compute_features -> score_risk -> decide

* ``submit_application`` validates the raw inputs (credit score in
  300-850, positive loan amount, non-negative DTI and tenure) and
  parks them on state.
* ``compute_features`` is the Hamilton-calling action. It loads the
  feature module from ``examples/data/hamilton_features/features.py``,
  builds a ``driver.Builder().with_modules(features).build()``, and
  runs ``execute(final_vars=[...], inputs={raw fields})``. Every
  derived node Hamilton returns is folded into Burr state.
* ``score_risk`` reads ``composite_risk_score`` (computed by Hamilton)
  and applies plain threshold rules to set the risk band.
* ``decide`` maps the risk band to approve / manual_review / deny and
  writes a final decision payload.

Hamilton has no idea it is running inside Burr inside an MCP server.
The Burr action body imports Hamilton, builds a driver, calls
execute(). No special integration, no shared abstractions: the
Hamilton module declares features with no Burr-specific anything,
and the Burr Application uses no Hamilton-specific anything except
that one call. ``mount()`` picks the Application up as-is; this
demo deliberately uses zero BurrMCP-specific glue beyond the same
``mount(build_application, mode=ServingMode.STEP)`` every other
example uses.

Run:

    uv run python examples/hamilton_features.py

A typical session:

    submit_application(applicant_id="A1", credit_score=720,
                       debt_to_income=0.25, employment_years=4.0,
                       loan_amount=20000)
    compute_features
    score_risk
    decide
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "hamilton-features-demo"
_FEATURES_PATH = Path(__file__).parent / "data" / "hamilton_features" / "features.py"
_FEATURES_MODULE_NAME = "hamilton_features_dag"

# Hamilton resolves nodes via ``sys.modules``, so the feature module
# must be registered there before the driver is built. Load it once,
# guarded by a lock, and cache the module object.
_features_lock = threading.Lock()
_features_module: Any = None


def _load_features_module() -> Any:
    """Load the Hamilton feature module from disk and cache it.

    The module is registered in ``sys.modules`` under
    ``_FEATURES_MODULE_NAME`` so Hamilton's graph walker can resolve
    upstream nodes by ``module.__name__``.
    """
    global _features_module
    with _features_lock:
        if _features_module is not None:
            return _features_module
        spec = importlib.util.spec_from_file_location(_FEATURES_MODULE_NAME, _FEATURES_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load Hamilton feature module at {_FEATURES_PATH}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_FEATURES_MODULE_NAME] = mod
        spec.loader.exec_module(mod)
        _features_module = mod
        return mod


# The list of Hamilton nodes the FSM materialises into Burr state.
# Adding a feature is a one-line change here plus a function in the
# Hamilton module; the FSM does not need to learn anything new.
_HAMILTON_FINAL_VARS = (
    "dti_bucket",
    "credit_tier",
    "employment_stability_score",
    "credit_risk_component",
    "dti_risk_component",
    "employment_risk_component",
    "composite_risk_score",
)


def _run_hamilton_features(
    credit_score: int, debt_to_income: float, employment_years: float
) -> dict[str, Any]:
    """Build a Hamilton driver and execute the feature DAG.

    Returns the dict of computed nodes keyed by node name. This is
    the only Hamilton-aware function in the file; everything else is
    plain Burr.
    """
    # Local import keeps Hamilton off the import path until the action
    # actually runs, mirroring how a production codebase would lazy-
    # load a feature library only inside the step that needs it.
    from hamilton import driver

    mod = _load_features_module()
    dr = driver.Builder().with_modules(mod).build()
    return dr.execute(
        list(_HAMILTON_FINAL_VARS),
        inputs={
            "credit_score": credit_score,
            "debt_to_income": debt_to_income,
            "employment_years": employment_years,
        },
    )


# Risk-band thresholds on the Hamilton-computed composite score.
# Tuned so the canonical mid-range applicant (credit 680, DTI 0.30,
# 3 years tenure) lands in "medium".
_LOW_RISK_MAX = 0.35
_MEDIUM_RISK_MAX = 0.55


def _risk_band_for(score: float) -> str:
    """Threshold the composite risk score into a band label."""
    if score < _LOW_RISK_MAX:
        return "low"
    if score < _MEDIUM_RISK_MAX:
        return "medium"
    return "high"


def _decision_for(risk_band: str) -> str:
    """Map a risk band to the final loan decision."""
    if risk_band == "low":
        return "approve"
    if risk_band == "medium":
        return "manual_review"
    return "deny"


# actions


@action(
    reads=[],
    writes=[
        "applicant_id",
        "credit_score",
        "debt_to_income",
        "employment_years",
        "loan_amount",
        "status",
        "log",
    ],
)
def submit_application(
    state: State,
    applicant_id: str,
    credit_score: int,
    debt_to_income: float,
    employment_years: float,
    loan_amount: float,
) -> State:
    """Validate the raw application inputs and park them on state.

    Args:
        applicant_id: Opaque identifier for the applicant; used only
            in the final decision payload.
        credit_score: FICO-like score in the inclusive range 300-850.
        debt_to_income: Ratio of monthly debt to monthly income.
            Must be non-negative; values above 1.0 are allowed (some
            applicants do owe more than they earn) but get scored
            harshly by the Hamilton DAG.
        employment_years: Years at current employer. Must be
            non-negative.
        loan_amount: Requested loan principal in USD. Must be
            strictly positive.
    """
    if not applicant_id:
        raise ValueError("applicant_id must be a non-empty string")
    if not (300 <= credit_score <= 850):
        raise ValueError("credit_score must be in [300, 850]")
    if debt_to_income < 0:
        raise ValueError("debt_to_income must be non-negative")
    if employment_years < 0:
        raise ValueError("employment_years must be non-negative")
    if loan_amount <= 0:
        raise ValueError("loan_amount must be positive")
    return state.update(
        applicant_id=applicant_id,
        credit_score=credit_score,
        debt_to_income=debt_to_income,
        employment_years=employment_years,
        loan_amount=loan_amount,
        status="submitted",
        log=[
            f"Submitted application {applicant_id}: credit_score={credit_score}, "
            f"debt_to_income={debt_to_income}, employment_years={employment_years}, "
            f"loan_amount={loan_amount}"
        ],
    )


@action(
    reads=["credit_score", "debt_to_income", "employment_years", "log"],
    writes=[
        "dti_bucket",
        "credit_tier",
        "employment_stability_score",
        "credit_risk_component",
        "dti_risk_component",
        "employment_risk_component",
        "composite_risk_score",
        "status",
        "log",
    ],
)
def compute_features(state: State) -> State:
    """Run the Hamilton feature DAG on the raw inputs.

    The whole call is three lines of Hamilton: build a driver from
    the feature module, run ``execute(final_vars, inputs)``, fold the
    returned dict into Burr state.
    """
    features = _run_hamilton_features(
        credit_score=state["credit_score"],
        debt_to_income=state["debt_to_income"],
        employment_years=state["employment_years"],
    )
    score = features["composite_risk_score"]
    return state.update(
        **features,
        status="features_computed",
        log=[
            *state["log"],
            (
                f"Computed Hamilton features: credit_tier={features['credit_tier']}, "
                f"dti_bucket={features['dti_bucket']}, "
                f"composite_risk_score={round(score, 4)}"
            ),
        ],
    )


@action(reads=["composite_risk_score", "log"], writes=["risk_band", "status", "log"])
def score_risk(state: State) -> State:
    """Threshold the Hamilton-computed composite score into a band."""
    band = _risk_band_for(state["composite_risk_score"])
    return state.update(
        risk_band=band,
        status="risk_scored",
        log=[
            *state["log"],
            f"Risk band: {band} (composite={round(state['composite_risk_score'], 4)})",
        ],
    )


@action(
    reads=["applicant_id", "risk_band", "composite_risk_score", "loan_amount", "log"],
    writes=["decision", "decision_payload", "status", "log"],
)
def decide(state: State) -> State:
    """Pick the final loan decision from the risk band."""
    decision = _decision_for(state["risk_band"])
    payload: dict[str, Any] = {
        "applicant_id": state["applicant_id"],
        "loan_amount": state["loan_amount"],
        "risk_band": state["risk_band"],
        "composite_risk_score": round(state["composite_risk_score"], 4),
        "decision": decision,
    }
    return state.update(
        decision=decision,
        decision_payload=payload,
        status="decided",
        log=[*state["log"], f"Decision: {decision} (risk_band={state['risk_band']})"],
    )


# graph


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            submit_application=submit_application,
            compute_features=compute_features,
            score_risk=score_risk,
            decide=decide,
        )
        .with_transitions(
            ("submit_application", "compute_features"),
            ("compute_features", "score_risk"),
            ("score_risk", "decide"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            applicant_id=None,
            credit_score=None,
            debt_to_income=None,
            employment_years=None,
            loan_amount=None,
            dti_bucket=None,
            credit_tier=None,
            employment_stability_score=None,
            credit_risk_component=None,
            dti_risk_component=None,
            employment_risk_component=None,
            composite_risk_score=None,
            risk_band=None,
            decision=None,
            decision_payload=None,
            status="initial",
            log=[],
        )
        .with_entrypoint("submit_application")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="hamilton-features",
        instructions=(
            "Loan-application risk scoring as a four-step FSM with one "
            "Hamilton-powered step. Walk: submit_application -> "
            "compute_features -> score_risk -> decide. "
            "submit_application(applicant_id, credit_score, "
            "debt_to_income, employment_years, loan_amount) validates "
            "inputs and parks them; compute_features invokes a Hamilton "
            "driver against the feature module at "
            "examples/data/hamilton_features/features.py to derive "
            "dti_bucket, credit_tier, employment_stability_score, and "
            "the composite_risk_score; score_risk thresholds the "
            "composite into low/medium/high; decide maps the band to "
            "approve/manual_review/deny. The Hamilton execution is "
            "opaque from MCP's perspective by design: its dataflow "
            "lives inside one Burr step, and the FSM around it "
            "provides the audit, gating, and retry story."
        ),
    )


if __name__ == "__main__":
    build_server().run()
