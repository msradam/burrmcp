"""Tests for examples/typed_state_loan.py.

Covers the load-bearing claims:
* The happy path walks submit -> underwrite -> decide and lands a
  decision.
* Pydantic field constraints reject out-of-range inputs via
  ``action_error`` with the full ValidationError message visible.
* The PydanticTypingSystem surfaces the model's JSON schema in
  ``burr://graph`` under ``state_schema``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from typed_state_loan import (  # noqa: E402
    LoanApplication,
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


def test_pydantic_model_carries_field_constraints():
    """The model declares the constraints; sanity-check they fire."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LoanApplication(credit_score=900)  # out of [300, 850]
    with pytest.raises(ValidationError):
        LoanApplication(debt_to_income=3.5)  # out of [0, 2.0]
    with pytest.raises(ValidationError):
        LoanApplication(loan_amount=-100)  # not > 0


def test_full_walk_lands_decision():
    app = build_application()
    _force_step(
        app,
        "submit_application",
        applicant_id="A1",
        credit_score=720,
        debt_to_income=0.25,
        employment_years=4.0,
        loan_amount=20000.0,
    )
    _force_step(app, "underwrite")
    _force_step(app, "decide")
    state = app.state
    assert state["stage"] == "decided"
    assert state["credit_tier"] in ("subprime", "near_prime", "prime", "super_prime")
    assert state["decision"] in ("approved", "manual_review", "denied")
    assert state["decision_reasons"]


def test_submit_rejects_out_of_range_credit_score():
    """Pydantic ValidationError raised by submit_application."""
    from pydantic import ValidationError

    app = build_application()
    with pytest.raises(ValidationError, match="credit_score"):
        _force_step(
            app,
            "submit_application",
            applicant_id="A2",
            credit_score=900,
            debt_to_income=0.30,
            employment_years=2.0,
            loan_amount=10000.0,
        )


def test_submit_rejects_negative_loan_amount():
    from pydantic import ValidationError

    app = build_application()
    with pytest.raises(ValidationError, match="loan_amount"):
        _force_step(
            app,
            "submit_application",
            applicant_id="A3",
            credit_score=720,
            debt_to_income=0.25,
            employment_years=4.0,
            loan_amount=-1.0,
        )


def test_underwriting_buckets_extremes_correctly():
    """Subprime + severe dti + no tenure should produce a denial."""
    app = build_application()
    _force_step(
        app,
        "submit_application",
        applicant_id="A_low",
        credit_score=520,
        debt_to_income=0.85,
        employment_years=0.5,
        loan_amount=30000.0,
    )
    _force_step(app, "underwrite")
    _force_step(app, "decide")
    assert app.state["credit_tier"] == "subprime"
    assert app.state["dti_bucket"] == "severe"
    assert app.state["decision"] == "denied"
    assert any("subprime" in r for r in app.state["decision_reasons"])


@pytest.mark.asyncio
async def test_graph_resource_exports_pydantic_schema():
    """burr://graph carries the LoanApplication JSON schema."""
    server = build_server()
    async with Client(server) as client:
        text = (await client.read_resource("burr://graph"))[0].text
        graph = json.loads(text)
        schema = graph.get("state_schema")
        assert schema is not None, "state_schema missing from burr://graph"
        # Pydantic JSON schemas have a "properties" dict.
        assert "properties" in schema
        props = schema["properties"]
        # Constraints surface in the per-field schema. Pydantic wraps
        # optional fields in `anyOf: [<typed schema>, {type: null}]`,
        # so the integer constraints live one level down.
        cs_options = props["credit_score"]["anyOf"]
        int_branch = next(b for b in cs_options if b.get("type") == "integer")
        assert int_branch.get("maximum") == 850
        assert int_branch.get("minimum") == 300
        dti_options = props["debt_to_income"]["anyOf"]
        num_branch = next(b for b in dti_options if b.get("type") == "number")
        assert num_branch.get("maximum") == 2.0


@pytest.mark.asyncio
async def test_mcp_step_walk_to_decision():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "submit_application",
                "inputs": {
                    "applicant_id": "A4",
                    "credit_score": 760,
                    "debt_to_income": 0.18,
                    "employment_years": 8.0,
                    "loan_amount": 50000.0,
                },
            },
        )
        await client.call_tool("step", {"action": "underwrite"})
        r = await client.call_tool("step", {"action": "decide"})
        out = json.loads(r.content[0].text)
        assert out["state"]["decision"] == "approved"
        assert out["valid_next_actions"] == []
