"""System under test (SUT): two percentile implementations.

Both functions compute "the p-th percentile of values" but with two
different conventions in widespread use. They agree on the median for
sorted inputs but diverge at non-median percentiles whenever the
position lands between two ranks. The combinatoric tester's job is to
hunt for inputs where the divergence is largest.

Conventions implemented:

* ``percentile_linear``: linear interpolation between adjacent ranks
  (the numpy / scipy default; ``numpy.percentile`` with method="linear").
* ``percentile_nearest``: nearest-rank method (Wikipedia's
  "Computing the percentile of a value" definition; equivalent to
  ``numpy.percentile(method="nearest")``).

Both reject empty lists and out-of-range p (must be in [0, 100]).
"""

from __future__ import annotations

import math


def percentile_linear(values: list[float], p: float) -> float:
    """p-th percentile by linear interpolation between adjacent ranks."""
    if not values:
        raise ValueError("values must be non-empty")
    if not 0 <= p <= 100:
        raise ValueError(f"p must be in [0, 100], got {p}")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return float(s[lo] + frac * (s[hi] - s[lo]))


def percentile_nearest(values: list[float], p: float) -> float:
    """p-th percentile by nearest-rank method.

    Returns the value at the rank ``ceil(p / 100 * len(values))``
    (one-indexed in the conventional definition, zero-indexed minus
    one in code).
    """
    if not values:
        raise ValueError("values must be non-empty")
    if not 0 <= p <= 100:
        raise ValueError(f"p must be in [0, 100], got {p}")
    s = sorted(values)
    n = len(s)
    rank = max(1, math.ceil(p / 100.0 * n))  # 1-indexed
    return float(s[rank - 1])
