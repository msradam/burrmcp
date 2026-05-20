"""Smoke tests for the CLI-wrapping git_review example.

Verifies the FSM walks correctly against the burrmcp repo itself
(which has commits and a clean working tree most of the time), and
that the SHA validator catches a fabricated commit hash.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from git_review import build_server


@pytest.mark.asyncio
async def test_full_git_review_walk():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "status", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["branch"]
        assert out["state"]["status_output"] is not None
        assert out["valid_next_actions"] == ["recent_commits"]

        r = await client.call_tool("step", {"action": "recent_commits", "inputs": {"count": 5}})
        out = json.loads(r.content[0].text)
        commits = out["state"]["recent_commits"]
        lines = commits.splitlines()
        assert len(lines) >= 1
        first_sha = lines[0].split()[0]
        assert out["valid_next_actions"] == ["show_commit"]

        # Loop on show_commit
        r = await client.call_tool(
            "step",
            {"action": "show_commit", "inputs": {"sha": first_sha, "done": False}},
        )
        out = json.loads(r.content[0].text)
        assert first_sha in out["state"]["commit_details"]
        assert out["state"]["done_inspecting"] is False
        assert out["valid_next_actions"] == ["show_commit"]

        # Finish inspecting; transition to summarize.
        r = await client.call_tool(
            "step",
            {"action": "show_commit", "inputs": {"sha": first_sha, "done": True}},
        )
        out = json.loads(r.content[0].text)
        assert out["valid_next_actions"] == ["summarize"]

        r = await client.call_tool("step", {"action": "summarize", "inputs": {}})
        out = json.loads(r.content[0].text)
        summary = out["state"]["summary"]
        assert summary["branch"]
        assert first_sha in summary["commits_inspected"]


@pytest.mark.asyncio
async def test_unknown_sha_rejected_by_validator():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "status", "inputs": {}})
        await client.call_tool("step", {"action": "recent_commits", "inputs": {"count": 3}})
        r = await client.call_tool(
            "step", {"action": "show_commit", "inputs": {"sha": "deadbeef0000"}}
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "validation_failed"
        assert "not in the most recent" in out["reason"]


@pytest.mark.asyncio
async def test_show_commit_without_recent_commits_first_refuses():
    server = build_server()
    async with Client(server) as client:
        # status -> recent_commits is the legal path; skip recent_commits
        # and try show_commit. The FSM gates this with invalid_transition
        # before the validator even runs.
        await client.call_tool("step", {"action": "status", "inputs": {}})
        r = await client.call_tool("step", {"action": "show_commit", "inputs": {"sha": "abcdef"}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["recent_commits"]
