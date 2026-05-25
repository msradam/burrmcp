"""Tests for examples/partition_key_tenants.py.

Validates that ``with_identifiers(partition_key=...)`` sets a real
partition_key on the live Application, and that switching the env
var produces isolated Applications. Also confirms the custom
``theodosia://tenant`` resource exposes the current tenant id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from partition_key_tenants import build_application, build_server


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_partition_key_defaults_to_default(monkeypatch):
    monkeypatch.delenv("BURRMCP_TENANT_ID", raising=False)
    app = build_application()
    assert app._partition_key == "default"


def test_partition_key_picks_up_env_var(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    app = build_application()
    assert app._partition_key == "acme"


def test_two_tenants_have_distinct_partition_keys(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    a = build_application()
    monkeypatch.setenv("BURRMCP_TENANT_ID", "globex")
    b = build_application()
    assert a._partition_key == "acme"
    assert b._partition_key == "globex"
    assert a.uid != b.uid  # Distinct app_ids too.


def test_full_walk_open_update_close(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    app = build_application()
    _force_step(app, "open_record", customer_id="C-001")
    _force_step(app, "update_note", note="first call")
    _force_step(app, "update_note", note="follow up")
    _force_step(app, "close_record")
    assert app.state["stage"] == "closed"
    assert len(app.state["notes"]) == 2


def test_update_note_refuses_in_closed_stage(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    app = build_application()
    _force_step(app, "open_record", customer_id="C-002")
    _force_step(app, "close_record")
    # The transition graph permits the LLM to call update_note again
    # only when stage == 'open'. After close, the action body refuses
    # if forced via _force_step (which bypasses transition routing).
    with pytest.raises(ValueError, match="cannot update notes"):
        _force_step(app, "update_note", note="too late")


def test_open_record_rejects_empty_customer_id(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    app = build_application()
    with pytest.raises(ValueError, match="customer_id"):
        _force_step(app, "open_record", customer_id="   ")


@pytest.mark.asyncio
async def test_tenant_resource_exposes_partition_key(monkeypatch):
    monkeypatch.setenv("BURRMCP_TENANT_ID", "acme")
    server = build_server()
    async with Client(server) as client:
        text = (await client.read_resource("theodosia://tenant"))[0].text
        info = json.loads(text)
        assert info["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_session_resource_carries_partition_key(monkeypatch):
    """Theodosia's theodosia://session resource exposes the partition_key
    set via with_identifiers."""
    monkeypatch.setenv("BURRMCP_TENANT_ID", "globex")
    server = build_server()
    async with Client(server) as client:
        text = (await client.read_resource("theodosia://session"))[0].text
        info = json.loads(text)
        assert info["partition_key"] == "globex"
