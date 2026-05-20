"""Fixtures shared across the smoke suite.

The headline fixture is ``flask_repo``: a session-scoped checkout of
Flask at a pinned tag, cached at ``~/.cache/burrmcp-smoke/flask`` so
repeat runs don't re-clone. The first run does a shallow clone (~30s);
subsequent runs reuse the cache instantly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Pinned for reproducibility across machines and time. Bump when we want
# a different baseline; nothing in the smoke tests depends on a specific
# commit's content, only that the checkout is a real Python codebase.
_FLASK_TAG = "3.0.3"
_FLASK_URL = "https://github.com/pallets/flask"
_CACHE_DIR = Path("~/.cache/burrmcp-smoke").expanduser()


@pytest.fixture(scope="session")
def flask_repo() -> Path:
    """Return the path to a Flask checkout, cloning on first use.

    Skips the test if `git` is unavailable or the clone fails (e.g.,
    no network). The cache survives between test sessions so only the
    first run pays the clone cost.
    """
    if shutil.which("git") is None:
        pytest.skip("flask_repo fixture requires `git` on PATH")
    repo = _CACHE_DIR / "flask"
    if repo.exists() and (repo / "src" / "flask").is_dir():
        return repo
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if repo.exists():
        shutil.rmtree(repo)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "--branch",
                _FLASK_TAG,
                _FLASK_URL,
                str(repo),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Could not clone Flask for smoke fixture: {exc}")
    if not (repo / "src" / "flask").is_dir():
        pytest.skip(f"Cloned Flask repo doesn't look right at {repo}")
    return repo
