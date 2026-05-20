"""Hamilton DAG for categorical combo testing.

Same shape as ``examples/data/combinatoric_testing/dag.py`` (param /
derivation / assertion nodes), but the params are categorical instead
of numeric. The two SUT calls are the derivation nodes; the assertion
node folds them into a structured ``matches`` verdict plus the dollar
delta when they disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from checkout import process_order, reference_process_order


def tier(tier_input: str) -> str:
    """Param node: customer tier."""
    return tier_input


def region(region_input: str) -> str:
    """Param node: customer region."""
    return region_input


def payment(payment_input: str) -> str:
    """Param node: payment method."""
    return payment_input


def coupon(coupon_input: str) -> str:
    """Param node: coupon applied."""
    return coupon_input


def quantity(quantity_input: int) -> int:
    """Param node: item quantity."""
    return int(quantity_input)


def base_price(base_price_input: float) -> float:
    """Param node: per-item base price."""
    return float(base_price_input)


def production_total(
    tier: str,
    region: str,
    payment: str,
    coupon: str,
    quantity: int,
    base_price: float,
) -> float:
    """Derivation: the production (buggy) implementation's output."""
    return process_order(tier, region, payment, coupon, quantity, base_price)


def reference_total(
    tier: str,
    region: str,
    payment: str,
    coupon: str,
    quantity: int,
    base_price: float,
) -> float:
    """Derivation: the reference (correct) implementation's output."""
    return reference_process_order(tier, region, payment, coupon, quantity, base_price)


def verdict(production_total: float, reference_total: float) -> dict[str, Any]:
    """Assertion: structured comparison."""
    delta = round(production_total - reference_total, 2)
    return {
        "production": production_total,
        "reference": reference_total,
        "delta": delta,
        "matches": abs(delta) < 0.01,
    }
