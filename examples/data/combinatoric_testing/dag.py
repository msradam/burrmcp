"""Hamilton DAG for differential percentile testing.

Three node classes, all required for the combinatoric-testing pattern:

* **Param nodes** (``values_input``, ``p_input``): pass-throughs that
  declare the LLM-fillable surface. Renaming them in the Burr action
  body makes the slot-fill schema readable in the MCP tool args.
* **Derivation nodes** (``v1_result``, ``v2_result``): two SUT calls
  given the same inputs.
* **Assertion nodes** (``divergence``): structured verdict on whether
  the two implementations disagreed.

The DAG runs deterministically for each combo; the LLM drives the
input space via Burr's ``step`` tool.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Hamilton loads this module via importlib.util.spec_from_file_location,
# so its parent dir isn't on sys.path by default. Add it so the
# sibling ``sut.py`` module is importable as a top-level name.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from sut import percentile_linear, percentile_nearest  # noqa: E402


def values(values_input: list[float]) -> list[float]:
    """Param node: the values list to compute the percentile over."""
    return list(values_input)


def p(p_input: float) -> float:
    """Param node: the percentile to compute (0 to 100)."""
    return float(p_input)


def v1_result(values: list[float], p: float) -> float:
    """Derivation: linear-interpolation percentile."""
    return percentile_linear(values, p)


def v2_result(values: list[float], p: float) -> float:
    """Derivation: nearest-rank percentile."""
    return percentile_nearest(values, p)


def divergence(v1_result: float, v2_result: float) -> dict[str, Any]:
    """Assertion: structured comparison of the two SUTs.

    Returns ``abs_diff`` (absolute difference) plus a ``diverges`` flag
    that the LLM can read off in one number to decide whether to keep
    searching this region of parameter space.
    """
    abs_diff = abs(v1_result - v2_result)
    return {
        "v1": v1_result,
        "v2": v2_result,
        "abs_diff": abs_diff,
        "diverges": abs_diff > 1e-9,
    }
