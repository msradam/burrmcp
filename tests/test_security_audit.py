"""Skill-to-FSM: web app security audit as caller-LLM prompts.

The FSM is pure orchestration, with no server-side LLM calls and
no shellouts, so tests just exercise the transitions and the
prompt-emission. The agent is simulated by feeding canned
``findings`` dicts/lists into each action's inputs and reading
``state.current_prompt`` afterward.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from security_audit import build_server


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


# ── start_audit validation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_audit_rejects_empty_target():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_audit", target="", mode="INSIDE"))
        assert out["error"] == "action_error"
        assert "target must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_start_audit_rejects_unknown_mode():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_audit", target="repo", mode="MAGIC"))
        assert out["error"] == "action_error"
        assert "mode must be one of" in out["error_message"]


@pytest.mark.asyncio
async def test_outside_mode_requires_authorization_source():
    """The SKILL says you must have written authorization before
    probing production. start_audit refuses without it."""
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(client, "start_audit", target="https://prod.example.com", mode="OUTSIDE")
        )
        assert out["error"] == "action_error"
        assert "authorization" in out["error_message"].lower()


@pytest.mark.asyncio
async def test_both_mode_requires_authorization_source():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_audit", target="my-repo", mode="BOTH"))
        assert out["error"] == "action_error"
        assert "authorization" in out["error_message"].lower()


@pytest.mark.asyncio
async def test_inside_mode_does_not_require_authorization_source():
    """INSIDE mode = sitting in your own codebase, no authorization
    needed."""
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_audit", target="my-repo", mode="INSIDE"))
        assert "error" not in out
        assert out["state"]["mode"] == "INSIDE"


# ── prompt emission ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_audit_emits_context_prompt_with_target_and_mode():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_audit", target="my-repo", mode="INSIDE"))
        prompt = out["state"]["current_prompt"]
        assert "CONTEXT DETECTION" in prompt
        assert "my-repo" in prompt
        assert "INSIDE" in prompt


# ── branching on mode ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inside_walk_skips_blackbox():
    """INSIDE goes: context -> source -> infra -> rate -> advisory.
    blackbox_review is not in any valid_next along the way."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="my-repo", mode="INSIDE")
        out = _payload(await _step(client, "record_context", findings={"stack": "python"}))
        assert out["valid_next_actions"] == ["source_review"]
        assert "SOURCE-CODE CHECKLIST" in out["state"]["current_prompt"]
        out = _payload(await _step(client, "source_review", findings=[]))
        assert out["valid_next_actions"] == ["infra_sweep"]
        out = _payload(await _step(client, "infra_sweep", findings=[]))
        assert out["valid_next_actions"] == ["rate_limit_deep_dive"]
        out = _payload(await _step(client, "rate_limit_deep_dive", findings=[]))
        assert out["valid_next_actions"] == ["write_advisory"]
        out = _payload(
            await _step(client, "write_advisory", advisory="# Empty advisory\n\nNothing found.\n")
        )
        assert out["state"]["audit_summary"]["mode"] == "INSIDE"
        assert out["state"]["audit_summary"]["total_findings"] == 0


@pytest.mark.asyncio
async def test_outside_walk_skips_source():
    """OUTSIDE goes: context -> blackbox -> infra -> rate -> advisory.
    source_review is not in any valid_next along the way."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_audit",
            target="https://prod.example.com",
            mode="OUTSIDE",
            authorization_source="GHSA collaborator invite from maintainer",
        )
        out = _payload(await _step(client, "record_context", findings={"server": "nginx"}))
        assert out["valid_next_actions"] == ["blackbox_review"]
        assert "BLACK-BOX CHECKLIST" in out["state"]["current_prompt"]
        # The authorization source must appear in the blackbox prompt.
        assert "GHSA collaborator invite" in out["state"]["current_prompt"]
        out = _payload(await _step(client, "blackbox_review", findings=[]))
        assert out["valid_next_actions"] == ["infra_sweep"]


@pytest.mark.asyncio
async def test_both_walk_runs_source_then_blackbox():
    """BOTH goes: context -> source -> blackbox -> infra -> rate -> advisory."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_audit",
            target="my-repo + https://staging.example.com",
            mode="BOTH",
            authorization_source="self-hosted internal app, audit blessed by SRE",
        )
        out = _payload(await _step(client, "record_context", findings={"stack": "node + react"}))
        assert out["valid_next_actions"] == ["source_review"]
        out = _payload(
            await _step(client, "source_review", findings=[{"cwe": "CWE-89", "severity": "high"}])
        )
        # In BOTH mode source_review continues to blackbox, not infra.
        assert out["valid_next_actions"] == ["blackbox_review"]
        assert "BLACK-BOX CHECKLIST" in out["state"]["current_prompt"]
        out = _payload(await _step(client, "blackbox_review", findings=[]))
        assert out["valid_next_actions"] == ["infra_sweep"]


# ── transition gating: out-of-order calls refused ──────────────────


@pytest.mark.asyncio
async def test_cannot_skip_to_write_advisory():
    """The agent can't jump from start_audit directly to write_advisory."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="x", mode="INSIDE")
        out = _payload(await _step(client, "write_advisory", advisory="premature"))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["record_context"]


@pytest.mark.asyncio
async def test_inside_mode_blackbox_review_refused():
    """Even after context in INSIDE mode, blackbox_review is unreachable."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="my-repo", mode="INSIDE")
        await _step(client, "record_context", findings={"stack": "python"})
        out = _payload(await _step(client, "blackbox_review", findings=[]))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["source_review"]


# ── findings flow into state ─────────────────────────────────────


@pytest.mark.asyncio
async def test_findings_accumulate_through_phases():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="my-repo", mode="INSIDE")
        await _step(client, "record_context", findings={"stack": "fastapi"})
        await _step(
            client,
            "source_review",
            findings=[
                {"file": "app/db.py", "line": 12, "cwe": "CWE-89", "severity": "high"},
                {"file": "app/admin.py", "line": 8, "cwe": "CWE-78", "severity": "critical"},
            ],
        )
        await _step(
            client,
            "infra_sweep",
            findings=[
                {"url": "https://prod/.env", "cwe": "CWE-538", "severity": "high"},
            ],
        )
        await _step(
            client,
            "rate_limit_deep_dive",
            findings=[
                {"url": "/api/login", "issue": "no rate limit on auth path", "severity": "medium"},
            ],
        )
        out = _payload(
            await _step(client, "write_advisory", advisory="# Advisory\n\n4 findings.\n")
        )
        summary = out["state"]["audit_summary"]
        assert summary["total_findings"] == 4
        assert summary["findings_per_phase"]["source"] == 2
        assert summary["findings_per_phase"]["infra"] == 1
        assert summary["findings_per_phase"]["rate_limit"] == 1
        assert summary["findings_per_phase"]["blackbox"] == 0


@pytest.mark.asyncio
async def test_advisory_must_not_be_empty():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="my-repo", mode="INSIDE")
        await _step(client, "record_context", findings={})
        await _step(client, "source_review", findings=[])
        await _step(client, "infra_sweep", findings=[])
        await _step(client, "rate_limit_deep_dive", findings=[])
        out = _payload(await _step(client, "write_advisory", advisory="   "))
        assert out["error"] == "action_error"
        assert "advisory must not be empty" in out["error_message"]


# ── audit trail in burr://history ────────────────────────────────


@pytest.mark.asyncio
async def test_history_records_each_phase():
    """Every phase is one history entry, in order."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", target="my-repo", mode="INSIDE")
        await _step(client, "record_context", findings={"stack": "python"})
        await _step(client, "source_review", findings=[])
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == ["start_audit", "record_context", "source_review"]
