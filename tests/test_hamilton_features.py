"""Burr-plus-Hamilton FSM: gating, math, and end-to-end walk.

Tests cover the Hamilton feature DAG in isolation, the input
validation on submit_application, that compute_features writes every
Hamilton-derived feature into Burr state, the threshold logic in
score_risk, the band-to-decision mapping in decide, the full
happy-path walk to the terminal, and that the FSM refuses
out-of-order calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from hamilton_features import (  # noqa: E402
    _HAMILTON_FINAL_VARS,
    _decision_for,
    _load_features_module,
    _risk_band_for,
    _run_hamilton_features,
    build_server,
)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


# Hamilton DAG math


def test_hamilton_dag_computes_expected_features_for_prime_applicant():
    """A clean prime borrower lands in the low-risk band."""
    out = _run_hamilton_features(credit_score=780, debt_to_income=0.18, employment_years=8.0)
    assert out["credit_tier"] == "super_prime"
    assert out["dti_bucket"] == "low"
    assert out["employment_stability_score"] == pytest.approx(0.8, abs=1e-9)
    assert out["credit_risk_component"] == pytest.approx(1.0 - (780 - 300) / 550.0, abs=1e-9)
    assert out["dti_risk_component"] == pytest.approx(0.18, abs=1e-9)
    assert out["employment_risk_component"] == pytest.approx(0.2, abs=1e-9)
    expected_composite = 0.5 * (1.0 - (780 - 300) / 550.0) + 0.3 * 0.18 + 0.2 * 0.2
    assert out["composite_risk_score"] == pytest.approx(expected_composite, abs=1e-9)
    assert out["composite_risk_score"] < 0.35  # low band


def test_hamilton_dag_returns_every_declared_final_var():
    """Sanity: the FSM's expected node list matches what the DAG emits."""
    out = _run_hamilton_features(credit_score=650, debt_to_income=0.30, employment_years=2.0)
    assert set(out) == set(_HAMILTON_FINAL_VARS)


def test_hamilton_dag_handles_severe_applicant_in_high_band():
    """High DTI and a subprime score push the composite past 0.55."""
    out = _run_hamilton_features(credit_score=520, debt_to_income=0.70, employment_years=0.5)
    assert out["credit_tier"] == "subprime"
    assert out["dti_bucket"] == "severe"
    assert out["composite_risk_score"] > 0.55


def test_load_features_module_is_idempotent():
    """The cached module load returns the same object on a second call."""
    m1 = _load_features_module()
    m2 = _load_features_module()
    assert m1 is m2
    assert hasattr(m1, "composite_risk_score")


# pure helpers


def test_risk_band_thresholds():
    assert _risk_band_for(0.10) == "low"
    assert _risk_band_for(0.34999) == "low"
    assert _risk_band_for(0.35) == "medium"
    assert _risk_band_for(0.54999) == "medium"
    assert _risk_band_for(0.55) == "high"
    assert _risk_band_for(0.99) == "high"


def test_decision_for_each_band():
    assert _decision_for("low") == "approve"
    assert _decision_for("medium") == "manual_review"
    assert _decision_for("high") == "deny"


# FSM: input validation


@pytest.mark.asyncio
async def test_submit_application_rejects_negative_credit_score():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "submit_application",
                applicant_id="A1",
                credit_score=-50,
                debt_to_income=0.20,
                employment_years=3.0,
                loan_amount=10000,
            )
        )
        assert out["error"] == "action_error"
        assert "credit_score" in out["error_message"]


@pytest.mark.asyncio
async def test_submit_application_rejects_zero_loan_amount():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "submit_application",
                applicant_id="A1",
                credit_score=700,
                debt_to_income=0.20,
                employment_years=3.0,
                loan_amount=0,
            )
        )
        assert out["error"] == "action_error"
        assert "loan_amount" in out["error_message"]


@pytest.mark.asyncio
async def test_submit_application_rejects_negative_dti():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "submit_application",
                applicant_id="A1",
                credit_score=700,
                debt_to_income=-0.05,
                employment_years=3.0,
                loan_amount=10000,
            )
        )
        assert out["error"] == "action_error"
        assert "debt_to_income" in out["error_message"]


# FSM: Hamilton integration


@pytest.mark.asyncio
async def test_compute_features_writes_every_hamilton_node_into_state():
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "submit_application",
            applicant_id="A1",
            credit_score=720,
            debt_to_income=0.25,
            employment_years=4.0,
            loan_amount=20000,
        )
        out = _payload(await _step(client, "compute_features"))
        state = out["state"]
        for var in _HAMILTON_FINAL_VARS:
            assert state[var] is not None, f"{var} should be written by compute_features"
        assert state["credit_tier"] == "prime"
        assert state["dti_bucket"] == "moderate"
        assert state["status"] == "features_computed"


# FSM: scoring + decision


@pytest.mark.asyncio
async def test_score_risk_low_band_routes_to_approve():
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "submit_application",
            applicant_id="A_low",
            credit_score=800,
            debt_to_income=0.10,
            employment_years=10.0,
            loan_amount=15000,
        )
        await _step(client, "compute_features")
        out = _payload(await _step(client, "score_risk"))
        assert out["state"]["risk_band"] == "low"
        out = _payload(await _step(client, "decide"))
        assert out["state"]["decision"] == "approve"


@pytest.mark.asyncio
async def test_score_risk_high_band_routes_to_deny():
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "submit_application",
            applicant_id="A_high",
            credit_score=520,
            debt_to_income=0.70,
            employment_years=0.5,
            loan_amount=30000,
        )
        await _step(client, "compute_features")
        out = _payload(await _step(client, "score_risk"))
        assert out["state"]["risk_band"] == "high"
        out = _payload(await _step(client, "decide"))
        assert out["state"]["decision"] == "deny"


@pytest.mark.asyncio
async def test_full_happy_path_walk_to_decision():
    """submit -> compute_features -> score_risk -> decide, terminal."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "submit_application",
            applicant_id="A_walk",
            credit_score=700,
            debt_to_income=0.32,
            employment_years=3.0,
            loan_amount=25000,
        )
        await _step(client, "compute_features")
        await _step(client, "score_risk")
        out = _payload(await _step(client, "decide"))
        state = out["state"]
        # Every Hamilton-computed feature survived to the terminal state.
        for var in _HAMILTON_FINAL_VARS:
            assert state[var] is not None
        # FSM-computed fields are present too.
        assert state["risk_band"] in {"low", "medium", "high"}
        assert state["decision"] in {"approve", "manual_review", "deny"}
        assert state["decision_payload"]["applicant_id"] == "A_walk"
        assert state["status"] == "decided"
        # decide is terminal: nothing further is valid.
        assert out["valid_next_actions"] == []


# FSM: gating


@pytest.mark.asyncio
async def test_decide_before_compute_features_is_refused():
    """Calling decide right after submit (skipping compute_features
    and score_risk) is an invalid_transition refusal."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "submit_application",
            applicant_id="A1",
            credit_score=700,
            debt_to_income=0.20,
            employment_years=3.0,
            loan_amount=10000,
        )
        out = _payload(await _step(client, "decide"))
        assert out["error"] == "invalid_transition"
        assert "compute_features" in out["valid_next_actions"]
