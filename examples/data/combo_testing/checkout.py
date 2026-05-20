"""SUT: a checkout pricing engine with seeded interaction bugs.

Two implementations:

* ``reference_process_order``: the known-correct version. Used as the
  oracle.
* ``process_order``: the "production" version with three seeded 2-way
  interaction bugs. Most single-dimension changes go through the
  correct code path; the bugs only fire when specific dimensions
  intersect.

Seeded bugs (none are detectable from any single input value; each
requires a 2-way combination):

1. ``tier="enterprise" AND coupon="loyalty"``: tier discount is
   applied twice in the discount stack.
2. ``region="APAC" AND coupon="seasonal"``: the regional FX rate is
   skipped (left at 1.0) instead of applying the 0.95 multiplier.
3. ``region="EU" AND payment="wire"``: the wire-transfer fee is
   subtracted instead of added.

This is textbook combinatorial testing: the bug surface is in the
2-way interactions, not in any single dimension. A pairwise covering
array would catch all three; random sampling needs many more trials;
an LLM that can hypothesise about which dimensions interact should
need only a handful.
"""

from __future__ import annotations

TIERS = ("free", "premium", "enterprise")
REGIONS = ("US", "EU", "APAC")
PAYMENTS = ("card", "wire", "crypto")
COUPONS = ("none", "seasonal", "loyalty")

_TIER_DISCOUNT = {"free": 0.0, "premium": 0.10, "enterprise": 0.20}
_COUPON_DISCOUNT = {"none": 0.0, "seasonal": 0.05, "loyalty": 0.15}
_FX_RATE = {"US": 1.0, "EU": 1.08, "APAC": 0.95}


def _wire_fee_rate(subtotal: float) -> float:
    """Wire transfers carry a flat $25 fee, expressed as a rate."""
    return 25.0 / max(subtotal, 1.0)


def _fee_rate(payment: str, subtotal: float) -> float:
    if payment == "card":
        return 0.029
    if payment == "wire":
        return _wire_fee_rate(subtotal)
    if payment == "crypto":
        return 0.01
    raise ValueError(f"unknown payment method: {payment}")


def reference_process_order(
    tier: str,
    region: str,
    payment: str,
    coupon: str,
    quantity: int,
    base_price: float,
) -> float:
    """Oracle implementation. No bugs; all interactions correct."""
    subtotal = base_price * quantity

    total_discount = min(
        _TIER_DISCOUNT[tier] + _COUPON_DISCOUNT[coupon],
        0.50,
    )
    after_discount = subtotal * (1 - total_discount)

    after_fx = after_discount * _FX_RATE[region]

    fee = after_fx * _fee_rate(payment, subtotal)
    return round(after_fx + fee, 2)


def process_order(
    tier: str,
    region: str,
    payment: str,
    coupon: str,
    quantity: int,
    base_price: float,
) -> float:
    """Production implementation with seeded 2-way interaction bugs."""
    subtotal = base_price * quantity

    # Bug 1: enterprise + loyalty stacks tier discount twice.
    tier_disc = _TIER_DISCOUNT[tier]
    coupon_disc = _COUPON_DISCOUNT[coupon]
    if tier == "enterprise" and coupon == "loyalty":
        total_discount = tier_disc + coupon_disc + tier_disc
    else:
        total_discount = tier_disc + coupon_disc
    total_discount = min(total_discount, 0.50)
    after_discount = subtotal * (1 - total_discount)

    # Bug 2: APAC + seasonal skips FX conversion.
    # if/else kept verbose (vs a ternary) so the bug shape is visible
    # at a glance; the SIM108 lint suggestion would obscure it.
    if region == "APAC" and coupon == "seasonal":
        fx_rate = 1.0
    else:
        fx_rate = _FX_RATE[region]
    after_fx = after_discount * fx_rate

    # Bug 3: EU + wire flips the fee sign.
    if region == "EU" and payment == "wire":
        rate = -_wire_fee_rate(subtotal)
    else:
        rate = _fee_rate(payment, subtotal)
    fee = after_fx * rate
    return round(after_fx + fee, 2)
