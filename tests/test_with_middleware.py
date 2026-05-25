"""Tests for examples/with_middleware.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from with_middleware import build_server


@pytest.mark.asyncio
async def test_custom_counter_middleware_fires_on_every_tool_call():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        await client.call_tool("step", {"action": "reset", "inputs": {}})
        text = (await client.read_resource("theodosia://tool-counts"))[0].text
        counts = json.loads(text)
        assert counts.get("step") == 3


@pytest.mark.asyncio
async def test_rate_limit_refuses_after_burst():
    """A tight cap surfaces as ToolError once the burst capacity is gone."""
    server = build_server(rate_limit_per_second=1.0)
    async with Client(server) as client:
        # Burst capacity defaults to 2 * max_requests_per_second, so a small
        # number of immediate calls should saturate it.
        outcomes = []
        for _ in range(8):
            try:
                r = await client.call_tool("step", {"action": "tick", "inputs": {}})
                outcomes.append(("ok", r.structured_content))
            except Exception as exc:
                outcomes.append(("error", str(exc)))
        errored = [o for o in outcomes if o[0] == "error"]
        assert errored, f"Expected at least one rate-limited call; got all ok: {outcomes}"
        assert any("Rate limit exceeded" in o[1] for o in errored), errored


@pytest.mark.asyncio
async def test_other_resources_still_work_with_middleware():
    """Middleware shouldn't break the theodosia:// resources."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        state = json.loads((await client.read_resource("theodosia://state"))[0].text)
        assert state["count"] == 1
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        assert len(history) == 1
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        assert {"tick", "reset"} == {a["name"] for a in graph["actions"]}
