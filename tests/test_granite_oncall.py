"""Granite-in-the-graph: on-call alert triage demo.

Tests monkey-patch ``_call_granite`` to return canned responses so
the FSM behavior is exercised hermetically (no Ollama required).
Covers the happy path, the retry-then-succeed loop, both
three-strikes routes to human (severity and service), output
normalisation (trailing punctuation, mixed case), the deterministic
runbook lookup, and that the transition layer advertises the right
``valid_next_actions`` mid-retry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import granite_oncall
from granite_oncall import _find_runbook, build_server


def _patch_granite(monkeypatch, *responses: str):
    """Replace ``_call_granite`` with a queue of canned responses.

    Each call pops the next response. Running out raises so a missing
    test fixture is obvious instead of hanging on a real Ollama call.
    """
    queue = list(responses)

    async def fake_call(prompt, *, system=None, model=None):
        if not queue:
            raise AssertionError("ran out of canned Granite responses")
        return queue.pop(0)

    monkeypatch.setattr(granite_oncall, "_call_granite", fake_call)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


# ── happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_classify_extract_suggest(monkeypatch):
    """Granite succeeds first try on both classifications. The FSM
    walks through to format_response with a real corpus match."""
    _patch_granite(monkeypatch, "P1", "auth")
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "report_alert",
            text="Spike in 401s from auth-service since 14:00 UTC",
        )
        await _step(client, "classify_severity")
        await _step(client, "extract_service")
        await _step(client, "suggest_runbook")
        out = _payload(await _step(client, "format_response"))
        final = out["state"]["final_response"]
        assert final["status"] == "triaged"
        assert final["severity"] == "P1"
        assert final["service"] == "auth"
        # The shipped runbooks corpus has auth-debug.md with many mentions.
        assert "auth-debug" in final["runbook"]["name"]


# ── retry loops ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_severity_retries_then_succeeds(monkeypatch):
    """First two Granite calls return malformed output, third returns
    valid P2. The FSM loops back to classify_severity twice, then
    advances to extract_service."""
    _patch_granite(
        monkeypatch,
        "I think this is high priority",  # bad
        "looks like P-zero",  # bad
        "P2",  # good
        "billing",  # service good
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "report_alert", text="Stripe webhook 500s climbing")
        out = _payload(await _step(client, "classify_severity"))
        assert out["state"]["severity"] is None
        assert len(out["state"]["severity_attempts"]) == 1
        # Retry transition is live; not yet routing to human.
        assert "classify_severity" in out["valid_next_actions"]
        assert "route_to_human" not in out["valid_next_actions"]
        out = _payload(await _step(client, "classify_severity"))
        assert out["state"]["severity"] is None
        out = _payload(await _step(client, "classify_severity"))
        assert out["state"]["severity"] == "P2"
        assert "extract_service" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_severity_three_strikes_routes_to_human(monkeypatch):
    """After three malformed Granite responses, the only valid next
    is route_to_human."""
    _patch_granite(monkeypatch, "uhh", "definitely a big deal", "i dunno")
    server = build_server()
    async with Client(server) as client:
        await _step(client, "report_alert", text="Server room on fire")
        await _step(client, "classify_severity")
        await _step(client, "classify_severity")
        out = _payload(await _step(client, "classify_severity"))
        assert out["state"]["severity"] is None
        assert len(out["state"]["severity_attempts"]) == 3
        assert out["valid_next_actions"] == ["route_to_human"]
        out = _payload(await _step(client, "route_to_human"))
        final = out["state"]["final_response"]
        assert final["status"] == "needs_human"
        assert len(final["severity_attempts"]) == 3


@pytest.mark.asyncio
async def test_unknown_service_three_strikes_routes_to_human(monkeypatch):
    """Severity works, but Granite can't pick a known service even
    after three tries. FSM still routes cleanly to human."""
    _patch_granite(
        monkeypatch,
        "P1",
        "frobinator",
        "blarfo-service",
        "the orchestration plane",
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "report_alert", text="weird thing happening")
        await _step(client, "classify_severity")
        await _step(client, "extract_service")
        await _step(client, "extract_service")
        out = _payload(await _step(client, "extract_service"))
        assert out["state"]["service"] is None
        assert out["valid_next_actions"] == ["route_to_human"]


# ── output parsing tolerance ───────────────────────────────────────


@pytest.mark.asyncio
async def test_response_with_extra_whitespace_and_punct_is_normalised(monkeypatch):
    """Granite often emits 'P1.' or '  P1\\n'. The parser strips and
    uppercases, so both pass validation."""
    _patch_granite(monkeypatch, "  P1.\n", "AUTH")
    server = build_server()
    async with Client(server) as client:
        await _step(client, "report_alert", text="auth issues")
        out = _payload(await _step(client, "classify_severity"))
        assert out["state"]["severity"] == "P1"
        out = _payload(await _step(client, "extract_service"))
        assert out["state"]["service"] == "auth"


# ── runbook lookup (no Granite needed) ─────────────────────────────


def test_runbook_lookup_matches_service_specific_doc():
    """auth -> auth-debug.md, deploy -> deploy-rollback.md."""
    auth = _find_runbook("auth")
    assert "auth-debug" in auth["name"]
    deploy = _find_runbook("deploy")
    assert "deploy-rollback" in deploy["name"]


def test_runbook_lookup_falls_back_to_incident_response_for_unmatched_service():
    """billing has no specific runbook in our corpus, so we fall back
    to incident-response.md rather than returning the first sorted entry."""
    billing = _find_runbook("billing")
    assert "incident-response" in billing["name"]


# ── input validation + cold-start advertise ────────────────────────


@pytest.mark.asyncio
async def test_report_alert_with_empty_text_is_refused(monkeypatch):
    _patch_granite(monkeypatch)
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "report_alert", text="   "))
        assert out["error"] == "action_error"
        assert "alert text must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_burr_next_advertises_retry_not_human_after_one_bad_response(monkeypatch):
    """After a single rejection, retry is valid and route_to_human is
    not yet. This is the transition-condition arithmetic working."""
    _patch_granite(monkeypatch, "I dunno man")
    server = build_server()
    async with Client(server) as client:
        await _step(client, "report_alert", text="x")
        await _step(client, "classify_severity")
        nxt = json.loads((await client.read_resource("theodosia://next"))[0].text)
        assert "classify_severity" in nxt
        assert "route_to_human" not in nxt
