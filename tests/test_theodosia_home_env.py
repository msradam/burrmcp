"""``THEODOSIA_HOME`` env var controls the tracker storage root.

Resolution order is: ``--home`` flag → ``THEODOSIA_HOME`` env →
``build_cli(home=...)`` default → ``~/.theodosia``. The env var slot was
requested by a senior-platform-eng dogfood persona (L) who hit the
"no sessions in ~/.theodosia; reading ~/.burr" hint on every invocation
in production. With the env set, the hint suppresses (the operator has
explicitly chosen) and the resolution skips the implicit ``~/.burr``
fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theodosia.cli import _resolve_home


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("THEODOSIA_HOME", raising=False)


def test_explicit_flag_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-invocation ``--home`` overrides the env."""
    monkeypatch.setenv("THEODOSIA_HOME", str(tmp_path / "from-env"))
    flag_home = tmp_path / "from-flag"
    assert _resolve_home(flag_home) == flag_home


def test_env_wins_over_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No flag passed → env value picks up."""
    env_home = tmp_path / "from-env"
    monkeypatch.setenv("THEODOSIA_HOME", str(env_home))
    assert _resolve_home(None) == env_home


def test_env_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env value goes through ``Path.expanduser`` like the rest."""
    monkeypatch.setenv("THEODOSIA_HOME", "~/.my-store")
    assert _resolve_home(None) == Path("~/.my-store").expanduser()


def test_env_suppresses_burr_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the env is set, the implicit ``~/.burr`` fallback does not fire,
    even if ``~/.theodosia`` would not exist and ``~/.burr`` would have content.
    The whole point of the env is "I have chosen explicitly; stop hinting."
    """
    env_home = tmp_path / "operator-chose-this"
    monkeypatch.setenv("THEODOSIA_HOME", str(env_home))
    # Force HOME to a tmp tree where ~/.theodosia is missing and ~/.burr has data.
    fake_home = tmp_path / "fake-user-home"
    fake_home.mkdir()
    (fake_home / ".burr").mkdir()
    (fake_home / ".burr" / "stuff").write_text("x")
    monkeypatch.setenv("HOME", str(fake_home))
    # Env value wins; we never see the ~/.burr path.
    assert _resolve_home(None) == env_home
