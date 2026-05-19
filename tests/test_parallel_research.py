"""Parallel sub-application spawn via asyncio.gather works concurrently.

Sub-runs spawned from one parent action via ``asyncio.gather`` run
in parallel (they don't serialize on the session lock; ``spawn_subapp``
intentionally doesn't acquire it). Each sub-run appears as its own
``burr://subruns/{id}`` entry, and the parent history entry lists
the spawned ids in ``subruns``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from parallel_research import build_server  # noqa: E402


@pytest.mark.asyncio
async def test_fan_out_records_one_subrun_per_source():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {
                    "query": "burr-mcp",
                    "sources": ["docs", "issues", "discussions"],
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert len(out["state"]["results"]) == 3
        assert "docs" in out["state"]["report"]
        assert "issues" in out["state"]["report"]
        assert "discussions" in out["state"]["report"]

        index = json.loads((await client.read_resource("burr://subruns"))[0].text)
        assert len(index) == 3
        labels = {entry["label"] for entry in index}
        assert labels == {"search-docs", "search-issues", "search-discussions"}


@pytest.mark.asyncio
async def test_parent_history_lists_all_spawned_subrun_ids():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {"query": "x", "sources": ["a", "b", "c", "d"]},
            },
        )
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        entry = history[0]
        assert entry["action"] == "research"
        assert len(entry["subruns"]) == 4
        assert len(entry["subrun_uris"]) == 4


@pytest.mark.asyncio
async def test_fan_out_runs_concurrently_not_serially():
    """Five sources at 0.05s each should take well under 0.25s if
    they ran serially; concurrent execution should land near 0.05s plus
    overhead. Generous threshold to avoid flakiness."""
    server = build_server()
    async with Client(server) as client:
        sources = [f"s{i}" for i in range(5)]
        t0 = time.monotonic()
        await client.call_tool(
            "step",
            {"action": "research", "inputs": {"query": "x", "sources": sources}},
        )
        elapsed = time.monotonic() - t0
        # Serial would be 5 * 0.05 = 0.25s minimum. Concurrent should be
        # well under that; allow 0.20s of headroom for test infrastructure.
        assert elapsed < 0.20, f"expected concurrent execution; took {elapsed:.3f}s"
