"""Shared fixtures.

The coffee-order example doubles as the canonical test FSM. Tests
build a fresh Application per test so state never leaks across cases.
"""

from __future__ import annotations

# Make examples importable without an editable install of the examples dir.
import sys
from pathlib import Path

import pytest

from burr_mcp import ServingMode

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from coffee_order import build_application, build_server  # noqa: E402


@pytest.fixture
def fresh_app():
    return build_application()


@pytest.fixture
def server_step():
    return build_server(ServingMode.STEP)


@pytest.fixture
def server_tools():
    return build_server(ServingMode.TOOLS)


@pytest.fixture
def server_dynamic():
    return build_server(ServingMode.DYNAMIC)
