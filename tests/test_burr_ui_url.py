"""Burr UI deep-link helper used by ``sessions show`` and ``status``."""

from __future__ import annotations

import pytest

from theodosia.cli import _burr_ui_url


@pytest.fixture(autouse=True)
def _clear_ui_env(monkeypatch: pytest.MonkeyPatch):
    """Don't let a developer's shell exports drive these assertions."""
    monkeypatch.delenv("BURR_UI_HOST", raising=False)
    monkeypatch.delenv("BURR_UI_PORT", raising=False)


def test_default_url_shape():
    url = _burr_ui_url("my-project", "abc123")
    assert url == "http://localhost:7241/project/my-project/null/abc123"


def test_explicit_partition_key():
    url = _burr_ui_url("p", "id", "tenant-a")
    assert url == "http://localhost:7241/project/p/tenant-a/id"


def test_host_and_port_override():
    url = _burr_ui_url("p", "id", host="ui.internal", port=8080)
    assert url == "http://ui.internal:8080/project/p/null/id"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch):
    """``BURR_UI_HOST`` / ``BURR_UI_PORT`` flow through for tunnel users."""
    monkeypatch.setenv("BURR_UI_HOST", "tunnel.example.com")
    monkeypatch.setenv("BURR_UI_PORT", "9999")
    url = _burr_ui_url("p", "id")
    assert url == "http://tunnel.example.com:9999/project/p/null/id"


def test_empty_string_partition_falls_back_to_null():
    """Treat empty-string partition as no-partition for the route."""
    url = _burr_ui_url("p", "id", "")
    assert url == "http://localhost:7241/project/p/null/id"
