"""Hamilton feature graph for loan-application risk scoring.

Each function below is a node in Hamilton's DAG. Function names are
node names; parameter names declare the upstream nodes that feed in.
Hamilton wires the graph from these signatures, so no decorator,
no registration call, no Burr import here. This module knows nothing
about FSMs or MCP; it is plain Hamilton.

Inputs (must be supplied via ``driver.execute(inputs=...)``):

    credit_score        int    300-850
    debt_to_income      float  0.0-1.5 (ratio, not percent)
    employment_years    float  years at current employer
    loan_amount         float  requested loan principal, USD

Derived nodes:

    dti_bucket          str    "low" | "moderate" | "high" | "severe"
    credit_tier         str    "subprime" | "near_prime" | "prime" | "super_prime"
    employment_stability_score  float  0.0-1.0
    composite_risk_score        float  0.0-1.0 (higher = riskier)
"""

from __future__ import annotations


def dti_bucket(debt_to_income: float) -> str:
    """Bucket the DTI ratio into a coarse band."""
    if debt_to_income < 0.20:
        return "low"
    if debt_to_income < 0.36:
        return "moderate"
    if debt_to_income < 0.50:
        return "high"
    return "severe"


def credit_tier(credit_score: int) -> str:
    """Map a FICO-like score to a tier label."""
    if credit_score < 580:
        return "subprime"
    if credit_score < 670:
        return "near_prime"
    if credit_score < 740:
        return "prime"
    return "super_prime"


def employment_stability_score(employment_years: float) -> float:
    """Saturating score on years of employment, capped at 1.0 by 10 years."""
    if employment_years <= 0:
        return 0.0
    return min(1.0, employment_years / 10.0)


def credit_risk_component(credit_score: int) -> float:
    """Higher score, lower risk. Normalised to 0.0-1.0."""
    clamped = max(300, min(850, credit_score))
    return 1.0 - (clamped - 300) / 550.0


def dti_risk_component(debt_to_income: float) -> float:
    """Higher DTI, higher risk. Saturates at DTI of 1.0."""
    return max(0.0, min(1.0, debt_to_income))


def employment_risk_component(employment_stability_score: float) -> float:
    """Inverse of the stability score: longer tenure means lower risk."""
    return 1.0 - employment_stability_score


def composite_risk_score(
    credit_risk_component: float,
    dti_risk_component: float,
    employment_risk_component: float,
) -> float:
    """Weighted blend of the three risk components.

    Weights chosen so credit history dominates (0.5), DTI matters next
    (0.3), and employment tenure rounds it out (0.2). The result is in
    [0.0, 1.0]; the FSM thresholds 0.35 and 0.55 split it into low /
    medium / high risk bands.
    """
    return 0.5 * credit_risk_component + 0.3 * dti_risk_component + 0.2 * employment_risk_component
