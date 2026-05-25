"""Pydantic-typed state via Burr's PydanticTypingSystem.

This demo's purpose is narrow: show what changes when you wire a
Pydantic model into ``ApplicationBuilder.with_typing(...)`` instead
of using the default untyped state. Everything else, including
mount(), the four MCP tools in STEP mode, the resources, and the
session machinery, is unchanged.

What typed state gives you, surfaced through Theodosia:

* **A real JSON Schema for state.** ``theodosia://graph`` exports the
  Pydantic model's full JSON schema under ``state_schema``. An MCP
  client introspecting the graph gets typed shape information --
  field names, types, constraints, enums -- without having to
  reason from each action's writes.
* **Constraint validation on demand.** Burr's ``State`` itself is a
  dict (it does not run Pydantic on every ``state.update`` call), so
  validation isn't automatic. The pattern this demo uses: construct
  the Pydantic model from the proposed inputs at the top of each
  write-side action. A ``ValidationError`` surfaces as ``action_error``
  through the Theodosia adapter, with the full Pydantic reason. The
  caller LLM sees structurally why its inputs were rejected and can
  self-correct, just like with any other action-body refusal.
* **One source of truth for shape.** The model declaration sits
  alongside the FSM; you don't have to re-state field types in
  action signatures, in the theodosia://graph payload, and in the caller
  LLM's instructions separately.

Domain: a tiny credit-scoring workflow. Three actions:

    submit_application -> underwrite -> decide

A real lender's logic would be much richer; this demo is the
shortest path that exercises constraint validation, literal types,
and a derived field that depends on validated inputs.

Run:

    uv run python examples/typed_state_loan.py
"""

from __future__ import annotations

from typing import Literal

from burr.core import ApplicationBuilder, State, action
from burr.integrations.pydantic import PydanticTypingSystem
from burr.tracking.client import LocalTrackingClient
from pydantic import BaseModel, Field

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "typed-state-loan-demo"


# == typed state =====================================================


CreditTier = Literal["subprime", "near_prime", "prime", "super_prime"]
Decision = Literal["approved", "manual_review", "denied"]
Stage = Literal["new", "submitted", "underwritten", "decided"]


class LoanApplication(BaseModel):
    """Pydantic-typed application state.

    Pydantic enforces field constraints at every ``state.update(...)``
    call. Out-of-range inputs (e.g., credit_score=900) raise inside
    the action body and surface as ``action_error`` over MCP.
    """

    stage: Stage = "new"
    applicant_id: str | None = None

    # Inputs. Ranges follow the standard US scoring system.
    credit_score: int | None = Field(default=None, ge=300, le=850)
    debt_to_income: float | None = Field(default=None, ge=0.0, le=2.0)
    employment_years: float | None = Field(default=None, ge=0.0)
    loan_amount: float | None = Field(default=None, gt=0.0)

    # Derived.
    credit_tier: CreditTier | None = None
    dti_bucket: Literal["low", "moderate", "high", "severe"] | None = None
    composite_risk_score: float | None = Field(default=None, ge=0.0, le=1.0)

    # Decision.
    decision: Decision | None = None
    decision_reasons: list[str] = Field(default_factory=list)


# == actions =========================================================


@action(
    reads=[],
    writes=[
        "stage",
        "applicant_id",
        "credit_score",
        "debt_to_income",
        "employment_years",
        "loan_amount",
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
    """Submit an application. Inputs are validated by constructing
    the ``LoanApplication`` Pydantic model; out-of-range values
    (``credit_score`` outside [300, 850], ``debt_to_income`` outside
    [0, 2.0], etc.) raise a ``ValidationError`` that the adapter
    surfaces as ``action_error`` with the full Pydantic reason.

    Args:
        applicant_id: Free-form identifier; carried through.
        credit_score: Score in [300, 850].
        debt_to_income: Ratio in [0, 2.0].
        employment_years: Non-negative tenure.
        loan_amount: Positive principal.
    """
    # Build the model from the proposed inputs; this triggers
    # Pydantic's field-level validation. Discard the model and just
    # write the validated values into state.
    LoanApplication(
        stage="submitted",
        applicant_id=applicant_id,
        credit_score=credit_score,
        debt_to_income=debt_to_income,
        employment_years=employment_years,
        loan_amount=loan_amount,
    )
    return state.update(
        stage="submitted",
        applicant_id=applicant_id,
        credit_score=credit_score,
        debt_to_income=debt_to_income,
        employment_years=employment_years,
        loan_amount=loan_amount,
    )


@action(
    reads=["credit_score", "debt_to_income", "employment_years"],
    writes=["stage", "credit_tier", "dti_bucket", "composite_risk_score"],
)
def underwrite(state: State) -> State:
    """Compute the derived fields from validated inputs.

    The risk score is a simple weighted blend the lender's policy
    team would tune in practice; the point of the demo is that the
    inputs are already-validated typed values, not raw Any.
    """
    cs = state["credit_score"]
    dti = state["debt_to_income"]
    yrs = state["employment_years"]

    if cs < 580:
        tier: CreditTier = "subprime"
    elif cs < 670:
        tier = "near_prime"
    elif cs < 740:
        tier = "prime"
    else:
        tier = "super_prime"

    if dti < 0.20:
        dti_bucket: Literal["low", "moderate", "high", "severe"] = "low"
    elif dti < 0.36:
        dti_bucket = "moderate"
    elif dti < 0.50:
        dti_bucket = "high"
    else:
        dti_bucket = "severe"

    credit_component = max(0.0, min(1.0, (850 - cs) / 550.0))
    dti_component = min(1.0, dti / 0.50)
    employment_component = max(0.0, 1.0 - min(yrs, 10.0) / 10.0)
    risk = round(
        0.6 * credit_component + 0.3 * dti_component + 0.1 * employment_component,
        3,
    )

    return state.update(
        stage="underwritten",
        credit_tier=tier,
        dti_bucket=dti_bucket,
        composite_risk_score=risk,
    )


@action(
    reads=["composite_risk_score", "credit_tier", "dti_bucket", "loan_amount"],
    writes=["stage", "decision", "decision_reasons"],
)
def decide(state: State) -> State:
    """Map risk + tier to a decision. Terminal."""
    risk = state["composite_risk_score"]
    tier = state["credit_tier"]
    dti_bucket = state["dti_bucket"]

    reasons: list[str] = []
    if risk < 0.35:
        decision: Decision = "approved"
        reasons.append(f"composite risk {risk:.3f} below 0.35 threshold")
    elif risk < 0.60:
        decision = "manual_review"
        reasons.append(f"composite risk {risk:.3f} in [0.35, 0.60) review band")
    else:
        decision = "denied"
        reasons.append(f"composite risk {risk:.3f} at/above 0.60 deny threshold")

    if tier == "subprime":
        reasons.append("credit tier subprime")
    if dti_bucket == "severe":
        reasons.append("dti severe (>=0.50)")

    return state.update(
        stage="decided",
        decision=decision,
        decision_reasons=reasons,
    )


# == graph ===========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_typing(PydanticTypingSystem(LoanApplication))
        .with_actions(
            submit_application=submit_application,
            underwrite=underwrite,
            decide=decide,
        )
        .with_transitions(
            ("submit_application", "underwrite"),
            ("underwrite", "decide"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(LoanApplication())
        .with_entrypoint("submit_application")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="typed-state-loan",
        instructions=(
            "A Pydantic-typed loan-scoring FSM. Walk: "
            "submit_application(applicant_id, credit_score, "
            "debt_to_income, employment_years, loan_amount) -> "
            "underwrite -> decide. The state is a Pydantic model "
            "(LoanApplication); every state.update() is validated, "
            "so out-of-range inputs (credit_score outside [300, 850], "
            "debt_to_income outside [0, 2.0]) raise inside the action "
            "and surface as action_error. theodosia://graph carries the "
            "full Pydantic JSON schema under state_schema so a client "
            "can introspect the typed shape without inferring from "
            "writes."
        ),
    )


if __name__ == "__main__":
    build_server().run()
