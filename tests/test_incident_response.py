"""Tests for examples/incident_response.py.

Exercises the real-ops-on-synthetic-data path: parsing the shipped
alert.json, slicing api-gateway.log to the alert window, cross-
referencing deploys.json, and walking the verify/mitigate loop with
real log evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from incident_response import (
    _build_investigation_subgraph,
    build_application,
    build_server,
)


async def _aforce_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        await app.astep(inputs=inputs or None)
    finally:
        app.get_next_action = original


@pytest.mark.asyncio
async def test_report_parses_real_alert_payload():
    app = build_application()
    await _aforce_step(app, "report")
    state = app.state
    assert state["service"] == "api-gateway"
    assert state["severity"] == "P2"
    assert state["pod"] == "api-gateway-7d8f9-xj2p4"
    assert state["alert_starts_at"] == "2026-05-20T14:22:00Z"
    assert "12.4%" in state["summary"]
    assert state["status"] == "reported"


@pytest.mark.asyncio
async def test_investigation_subgraph_reads_real_logs_and_deploys():
    sub = _build_investigation_subgraph(
        service="api-gateway", alert_starts_at="2026-05-20T14:22:00Z"
    )
    await _aforce_step(sub, "gather_logs", time_window_minutes=10)
    await _aforce_step(sub, "correlate_events")
    await _aforce_step(sub, "form_hypothesis")
    await _aforce_step(sub, "report_findings")

    log_window = sub.state["log_window"]
    # The log file's 14:12-14:32 slice covers warmup + deploy + errors.
    assert log_window["line_count"] > 30
    assert log_window["level_counts"]["ERROR"] >= 15
    assert log_window["level_counts"]["WARN"] >= 1

    correlations = sub.state["correlations"]
    # v2.14.3 started 6 min before the alert, within the 30-min window.
    candidate_ids = {d["id"] for d in correlations["candidate_deploys"]}
    assert "v2.14.3" in candidate_ids
    # Its log lines actually mention the deploy id during rollout.
    mentioned = {m["deploy_id"] for m in correlations["deploy_mentions_in_window"]}
    assert "v2.14.3" in mentioned

    hyp = sub.state["hypothesis"]
    assert "v2.14.3" in hyp
    assert "regression" in hyp


@pytest.mark.asyncio
async def test_full_walk_with_late_mitigation_verifies():
    """A rollback recorded at the recovery point should verify successfully."""
    app = build_application()
    await _aforce_step(app, "report")
    await _aforce_step(app, "acknowledge", responder="alice")
    await _aforce_step(app, "investigate")
    # Rollback completed at log time 14:30:20 (alert was 14:22:00, so +8m).
    # Mitigation at +9m puts the verify window in clean traffic.
    await _aforce_step(
        app,
        "mitigate",
        action_kind="rollback",
        target="v2.14.2",
        simulated_offset_minutes=9,
    )
    # Agent reads theodosia://state and sees the evidence; supplies verified.
    await _aforce_step(app, "verify", verified=True, notes="error count zero post-rollback")
    evidence = app.state["verification_evidence"]
    assert evidence["error_count"] == 0
    assert evidence["info_count"] > 0
    assert app.state["status"] == "verified"


@pytest.mark.asyncio
async def test_full_walk_with_early_mitigation_loops_back():
    """A mitigation recorded too early sees lingering errors; agent loops."""
    app = build_application()
    await _aforce_step(app, "report")
    await _aforce_step(app, "acknowledge", responder="bob")
    await _aforce_step(app, "investigate")
    # +2 min puts the post-window at 14:24-14:34, which still spans the
    # error region and only catches a slice of recovery.
    await _aforce_step(
        app,
        "mitigate",
        action_kind="rollback",
        target="v2.14.2",
        simulated_offset_minutes=2,
    )
    await _aforce_step(
        app, "verify", verified=False, notes="errors still present, retry mitigation"
    )
    evidence = app.state["verification_evidence"]
    assert evidence["error_count"] >= 5
    assert app.state["status"] == "verification_failed"

    # Loop: apply a later mitigation and verify again.
    await _aforce_step(
        app,
        "mitigate",
        action_kind="rollback",
        target="v2.14.2",
        simulated_offset_minutes=9,
    )
    await _aforce_step(app, "verify", verified=True, notes="rollback held")
    assert app.state["verification_evidence"]["error_count"] == 0
    assert app.state["status"] == "verified"


@pytest.mark.asyncio
async def test_mitigate_rejects_unknown_action_kind():
    app = build_application()
    await _aforce_step(app, "report")
    await _aforce_step(app, "acknowledge", responder="carol")
    await _aforce_step(app, "investigate")
    with pytest.raises(ValueError, match="action_kind must be"):
        await _aforce_step(
            app,
            "mitigate",
            action_kind="prayer",
            target="v2.14.2",
        )


@pytest.mark.asyncio
async def test_full_walk_through_mcp_step():
    """End-to-end through MCP: alert -> investigate -> rollback -> verify -> close."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "report", "inputs": {}})
        await client.call_tool("step", {"action": "acknowledge", "inputs": {"responder": "dave"}})
        r_invest = await client.call_tool("step", {"action": "investigate", "inputs": {}})
        invest_payload = r_invest.structured_content
        # Investigation should have populated findings + hypothesis.
        assert invest_payload["state"]["findings"]
        assert "v2.14.3" in invest_payload["state"]["hypothesis"]
        # Sub-run id appears in the response somewhere.
        history_text = (await client.read_resource("theodosia://history"))[0].text
        assert "investigate" in history_text
        subruns_text = (await client.read_resource("theodosia://subruns"))[0].text
        subruns = json.loads(subruns_text)
        assert subruns, "expected at least one subrun"

        await client.call_tool(
            "step",
            {
                "action": "mitigate",
                "inputs": {
                    "action_kind": "rollback",
                    "target": "v2.14.2",
                    "simulated_offset_minutes": 9,
                },
            },
        )
        r_verify = await client.call_tool(
            "step",
            {"action": "verify", "inputs": {"verified": True, "notes": "clean post-rollback"}},
        )
        verify_payload = r_verify.structured_content
        assert verify_payload["state"]["verification_evidence"]["error_count"] == 0
        await client.call_tool(
            "step", {"action": "resolve", "inputs": {"resolution": "rolled back to v2.14.2"}}
        )
        r_pm = await client.call_tool(
            "step",
            {
                "action": "write_postmortem",
                "inputs": {"postmortem_md": "# Postmortem\n\nv2.14.3 broke checkout."},
            },
        )
        pm = r_pm.structured_content
        assert pm["state"]["status"] == "closed"
