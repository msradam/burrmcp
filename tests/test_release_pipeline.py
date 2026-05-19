"""Release-pipeline FSM: the canonical "agent refuses to skip steps" demo.

Tests cover the happy path, the killer demo prompt (trying to call
``promote_to_prod`` first), tests-fail rewind, degraded-canary
rollback gating, and that observe_canary needs at least two
observations before promotion unlocks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from release_pipeline import build_server  # noqa: E402


async def _submit(client, summary="add metric to dashboard", risk="low"):
    await client.call_tool(
        "step",
        {
            "action": "submit_change",
            "inputs": {"summary": summary, "risk": risk},
        },
    )


@pytest.mark.asyncio
async def test_happy_path_through_promote_and_close():
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        await client.call_tool("step", {"action": "run_tests", "inputs": {"result": "passed"}})
        await client.call_tool("step", {"action": "deploy_canary", "inputs": {}})
        await client.call_tool(
            "step", {"action": "observe_canary", "inputs": {"status": "healthy"}}
        )
        await client.call_tool(
            "step", {"action": "observe_canary", "inputs": {"status": "healthy"}}
        )
        r = await client.call_tool("step", {"action": "promote_to_prod", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["promoted"] is True
        r = await client.call_tool("step", {"action": "close_change", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["stage"] == "closed"


@pytest.mark.asyncio
async def test_cannot_promote_before_submit():
    """The killer demo: agent tries to promote a hotfix directly."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "promote_to_prod", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["submit_change"]


@pytest.mark.asyncio
async def test_cannot_deploy_canary_with_failing_tests():
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        await client.call_tool("step", {"action": "run_tests", "inputs": {"result": "failed"}})
        # After a failed test run, the only valid next is submit_change
        # (which resets the pipeline so the author can try again).
        r = await client.call_tool("step", {"action": "deploy_canary", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["submit_change"]


@pytest.mark.asyncio
async def test_degraded_observation_forces_rollback_not_promote():
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        await client.call_tool("step", {"action": "run_tests", "inputs": {"result": "passed"}})
        await client.call_tool("step", {"action": "deploy_canary", "inputs": {}})
        await client.call_tool(
            "step", {"action": "observe_canary", "inputs": {"status": "degraded"}}
        )
        # Now promote_to_prod is not valid, only rollback is.
        r = await client.call_tool("step", {"action": "promote_to_prod", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["rollback"]


@pytest.mark.asyncio
async def test_one_healthy_observation_not_enough_to_promote():
    """Promotion requires at least two consecutive healthy observations."""
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        await client.call_tool("step", {"action": "run_tests", "inputs": {"result": "passed"}})
        await client.call_tool("step", {"action": "deploy_canary", "inputs": {}})
        await client.call_tool(
            "step", {"action": "observe_canary", "inputs": {"status": "healthy"}}
        )
        r = await client.call_tool("step", {"action": "promote_to_prod", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        # Need another observation first.
        assert "observe_canary" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_rollback_then_close_marks_outcome_rolled_back():
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        await client.call_tool("step", {"action": "run_tests", "inputs": {"result": "passed"}})
        await client.call_tool("step", {"action": "deploy_canary", "inputs": {}})
        await client.call_tool(
            "step", {"action": "observe_canary", "inputs": {"status": "degraded"}}
        )
        await client.call_tool("step", {"action": "rollback", "inputs": {}})
        r = await client.call_tool("step", {"action": "close_change", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["rolled_back"] is True
        assert out["state"]["promoted"] is False
        assert "rolled_back" in out["state"]["log"][-1]


@pytest.mark.asyncio
async def test_burr_next_advertises_only_legal_action():
    """Read burr://next mid-pipeline and confirm it points to the
    one currently-legal step, not every action."""
    server = build_server()
    async with Client(server) as client:
        await _submit(client)
        nxt = json.loads((await client.read_resource("burr://next"))[0].text)
        # After submit, the only valid next is run_tests.
        assert nxt == ["run_tests"]
