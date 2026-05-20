"""Branching FSM: conditional transitions are gated correctly.

The triage example branches out of ``classify`` based on ``severity``.
These tests verify that ``burr://next`` returns only the branch matching
current state, and that calls to other branches are refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from triage import build_application, build_server


def test_next_actions_after_classify_urgent_lists_only_escalate():
    import asyncio

    app = build_application()
    asyncio.run(app.astep(inputs={"subject": "down", "body": "site is 500"}))
    asyncio.run(app.astep(inputs={"severity": "urgent"}))

    from burrmcp.adapter import valid_next_action_names

    assert valid_next_action_names(app) == ["escalate"]


def test_next_actions_after_classify_routine_lists_only_queue():
    import asyncio

    app = build_application()
    asyncio.run(app.astep(inputs={"subject": "ask", "body": "how do I X"}))
    asyncio.run(app.astep(inputs={"severity": "routine"}))

    from burrmcp.adapter import valid_next_action_names

    assert valid_next_action_names(app) == ["queue"]


def test_next_actions_after_classify_spam_lists_only_drop():
    import asyncio

    app = build_application()
    asyncio.run(app.astep(inputs={"subject": "buy now", "body": "click here"}))
    asyncio.run(app.astep(inputs={"severity": "spam"}))

    from burrmcp.adapter import valid_next_action_names

    assert valid_next_action_names(app) == ["drop"]


@pytest.mark.asyncio
async def test_step_refuses_wrong_branch():
    """Classified as routine; trying to call escalate is rejected."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "intake", "inputs": {"subject": "x", "body": "y"}}
        )
        await client.call_tool("step", {"action": "classify", "inputs": {"severity": "routine"}})
        r = await client.call_tool("step", {"action": "escalate", "inputs": {"oncall": "alice"}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["queue"]


@pytest.mark.asyncio
async def test_step_allows_correct_branch():
    """Classified as urgent; escalate succeeds."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "intake", "inputs": {"subject": "down", "body": "500"}}
        )
        await client.call_tool("step", {"action": "classify", "inputs": {"severity": "urgent"}})
        r = await client.call_tool("step", {"action": "escalate", "inputs": {"oncall": "alice"}})
        out = json.loads(r.content[0].text)
        assert out["state"]["stage"] == "escalated"
        assert out["state"]["ticket_id"] == "INC-ALICE-001"
        assert out["valid_next_actions"] == []
