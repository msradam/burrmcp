"""Smoke test for the incident-response sample.

This is the example we point Claude Code at. It needs to work
end-to-end. Verify the full happy path plus one refusal of each
class the sample is designed to demonstrate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from incident_response import build_server  # noqa: E402


@pytest.mark.asyncio
async def test_full_happy_path():
    server = build_server()
    async with Client(server) as client:
        # 1. Report
        r = await client.call_tool(
            "step",
            {
                "action": "report",
                "inputs": {
                    "summary": "API 500s",
                    "severity": "P1",
                    "reporter": "alice",
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "reported"
        assert out["state"]["severity"] == "P1"
        assert out["state"]["incident_id"].startswith("INC-")
        assert out["valid_next_actions"] == ["acknowledge"]

        # 2. Acknowledge
        r = await client.call_tool(
            "step",
            {"action": "acknowledge", "inputs": {"responder": "bob"}},
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "acknowledged"
        assert out["state"]["responder"] == "bob"

        # 3. Investigate (spawns sub-graph)
        r = await client.call_tool(
            "step",
            {"action": "investigate", "inputs": {}},
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "investigated"
        # Sub-run was recorded.
        subruns = json.loads((await client.read_resource("burr://subruns"))[0].text)
        assert len(subruns) == 1
        assert subruns[0]["parent_action"] == "investigate"

        # 4. Mitigate
        r = await client.call_tool(
            "step",
            {"action": "mitigate", "inputs": {"mitigation": "rollback to 89a2"}},
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "mitigated"

        # 5. Verify (with verified=True; should advance to resolve)
        r = await client.call_tool(
            "step",
            {"action": "verify", "inputs": {"verified": True, "notes": "ok"}},
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["verified"] is True
        assert out["valid_next_actions"] == ["resolve"]

        # 6. Resolve
        r = await client.call_tool(
            "step",
            {"action": "resolve", "inputs": {"resolution": "rolled back"}},
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "resolved"

        # 7. Write postmortem
        r = await client.call_tool(
            "step",
            {
                "action": "write_postmortem",
                "inputs": {"postmortem_md": "# Postmortem\nRoot cause: bad deploy."},
            },
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["status"] == "closed"
        assert out["valid_next_actions"] == []  # terminal


@pytest.mark.asyncio
async def test_verify_false_loops_back_to_mitigate():
    server = build_server()
    async with Client(server) as client:
        for step, inputs in [
            ("report", {"summary": "x", "severity": "P2", "reporter": "alice"}),
            ("acknowledge", {"responder": "bob"}),
            ("investigate", {}),
            ("mitigate", {"mitigation": "first attempt"}),
            ("verify", {"verified": False, "notes": "still failing"}),
        ]:
            await client.call_tool("step", {"action": step, "inputs": inputs})

        # After verify(False) the graph loops back to mitigate.
        valid = json.loads((await client.read_resource("burr://next"))[0].text)
        assert valid == ["mitigate"]

        # Second attempt at mitigation, then successful verify, then resolve.
        await client.call_tool(
            "step",
            {"action": "mitigate", "inputs": {"mitigation": "second attempt"}},
        )
        r = await client.call_tool(
            "step",
            {"action": "verify", "inputs": {"verified": True}},
        )
        out = json.loads(r.content[0].text)
        assert out["valid_next_actions"] == ["resolve"]


@pytest.mark.asyncio
async def test_invalid_severity_is_refused_by_validator():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "report",
                "inputs": {
                    "summary": "x",
                    "severity": "Sev-2",  # not one of P1/P2/P3
                    "reporter": "alice",
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "validation_failed"
        assert out["reason"] == "severity must be one of P1, P2, P3"
        assert out["details"]["allowed"] == ["P1", "P2", "P3"]


@pytest.mark.asyncio
async def test_out_of_order_call_is_refused():
    server = build_server()
    async with Client(server) as client:
        # FSM is at entrypoint (report); resolve isn't reachable.
        r = await client.call_tool(
            "step",
            {"action": "resolve", "inputs": {"resolution": "skip"}},
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["report"]


@pytest.mark.asyncio
async def test_history_records_full_audit_trail():
    server = build_server()
    async with Client(server) as client:
        # One refusal then a success.
        await client.call_tool(
            "step",
            {"action": "resolve", "inputs": {"resolution": "early"}},
        )
        await client.call_tool(
            "step",
            {
                "action": "report",
                "inputs": {
                    "summary": "x",
                    "severity": "P3",
                    "reporter": "alice",
                },
            },
        )

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 2
        assert history[0]["refused"] is True
        assert history[0]["refusal_reason"] == "invalid_transition"
        assert history[1]["refused"] is False
        assert history[1]["action"] == "report"
        assert history[1]["state_after"]["status"] == "reported"
