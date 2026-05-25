"""Shared fixtures.

The coffee-order example doubles as the canonical test FSM. Tests
build a fresh Application per test so state never leaks across cases.

Example servers that wire a ``LocalTrackingClient`` would otherwise
write log files into the dev's real ``~/.burr/`` on every test run.
The ``_isolate_burr_home`` autouse fixture redirects ``HOME`` to a
per-test ``tmp_path`` so any tracker activity ends up under that
directory and gets cleaned up with the rest of the temp tree. Tests
that explicitly need to control ``HOME`` (e.g. ``test_fork_from_past``)
override it via their own ``monkeypatch.setenv`` call; the explicit
setting wins.
"""

from __future__ import annotations

# Make examples importable without an editable install of the examples dir.
import os
import sys
from pathlib import Path

# Typer renders help through Rich. With color on, Rich styles an option as
# separate spans ("-\x1b[0m\x1b[1m-mode"), so substring assertions like
# `"--mode" in output` fail; they pass locally only because Rich emits plain
# text to a non-tty. CI forces color, so pin plain output here: drop the
# force-color vars (they beat NO_COLOR), set NO_COLOR, and pin a wide width
# so nothing wraps. Makes help text deterministic regardless of environment.
for _force in ("FORCE_COLOR", "CLICOLOR_FORCE"):
    os.environ.pop(_force, None)
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ["COLUMNS"] = "200"

import pytest

from theodosia import ServingMode

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from coffee_order import build_application, build_server


@pytest.fixture(autouse=True)
def _isolate_burr_home(request, monkeypatch, tmp_path):
    # Smoke tests under tests/smoke/ drive a real Claude session via the
    # Agent SDK; they need the real $HOME so the SDK can read OAuth from
    # the platform keychain (or ~/.claude/.credentials.json) and resolve
    # ~/theodosia-demo/.mcp.json against the user's actual home.
    if "smoke" in request.keywords:
        return
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def fresh_app():
    return build_application()


@pytest.fixture
def server_step():
    return build_server(ServingMode.STEP)
