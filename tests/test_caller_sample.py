"""Tests for examples/caller_sample.py.

Drives the FSM through MCP with a fake sampling handler attached to
the FastMCP test Client. The handler intercepts each
``sampling/createMessage`` request and returns canned text, so the
demo's ``ctx.sample`` calls land in deterministic test territory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from caller_sample import build_server


def _make_handler(responses: list[str]):
    """Build a sampling handler that returns the next canned response per call."""
    iter_responses = iter(responses)

    def handler(messages, params, context) -> str:
        try:
            return next(iter_responses)
        except StopIteration as exc:
            raise AssertionError(
                "sampling handler called more times than canned responses provided"
            ) from exc

    return handler


@pytest.mark.asyncio
async def test_compose_delegates_to_caller_llm():
    server = build_server()
    handler = _make_handler(["Quantum computing is fast. Photons help."])
    async with Client(server, sampling_handler=handler) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "compose",
                "inputs": {"topic": "quantum computing", "style": "concise"},
            },
        )
        out = r.structured_content
        assert out.get("error") is None, out
        assert out["state"]["draft"] == "Quantum computing is fast. Photons help."
        assert out["state"]["topic"] == "quantum computing"
        assert out["state"]["style"] == "concise"


@pytest.mark.asyncio
async def test_revise_loops_with_caller_llm():
    server = build_server()
    handler = _make_handler(
        [
            "First draft about cats.",
            "Concise draft about cats.",
            "Very concise: cats.",
        ]
    )
    async with Client(server, sampling_handler=handler) as client:
        await client.call_tool(
            "step", {"action": "compose", "inputs": {"topic": "cats", "style": "default"}}
        )
        await client.call_tool(
            "step", {"action": "revise", "inputs": {"direction": "more concise"}}
        )
        r = await client.call_tool(
            "step", {"action": "revise", "inputs": {"direction": "even shorter"}}
        )
        out = r.structured_content
        assert out.get("error") is None, out
        assert out["state"]["draft"] == "Very concise: cats."
        revisions = out["state"]["revisions"]
        assert len(revisions) == 2
        assert revisions[0]["direction"] == "more concise"
        assert revisions[1]["direction"] == "even shorter"


@pytest.mark.asyncio
async def test_revise_refuses_when_no_draft():
    server = build_server()
    handler = _make_handler([])
    async with Client(server, sampling_handler=handler) as client:
        # revise requires compose to have run first; calling out of order
        # triggers Burr's transition refusal.
        r = await client.call_tool("step", {"action": "revise", "inputs": {"direction": "tighter"}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"


@pytest.mark.asyncio
async def test_sample_helper_returns_none_outside_action(monkeypatch):
    """current_mcp_context returns None outside an action body."""
    from theodosia import current_mcp_context

    assert current_mcp_context() is None
