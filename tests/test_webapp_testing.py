"""Tests for examples/webapp_testing.py.

Hermetic: no actual browser involved. The FSM stores observations
and assertions as data; the caller LLM is responsible for the real
Playwright driving in a live deployment. These tests exercise:

* Input validation (empty url, invalid app_kind, invalid verdict).
* The load-bearing rule: reconnaissance refuses when loaded=False.
* Full walks (happy path and a wait_for_load=False detour).
* MCP wire roundtrip + that wait_for_load false loops back rather
  than advancing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from webapp_testing import (
    build_server,
    finalize_test,
    reconnaissance,
    start_test,
    wait_for_load,
)


def _initial_state(**overrides):
    from burr.core.state import State

    base = {
        "target_url": "",
        "app_kind": "dynamic",
        "loaded": False,
        "observations": {},
        "selectors": {},
        "actions": [],
        "assertions": [],
        "verdict": None,
        "summary": None,
        "current_prompt": "",
        "log": [],
    }
    base.update(overrides)
    return State(base)


# == unit tests ===================================================


def test_start_test_rejects_empty_url():
    with pytest.raises(ValueError):
        start_test(_initial_state(), target_url="  ")


def test_start_test_rejects_invalid_app_kind():
    with pytest.raises(ValueError):
        start_test(
            _initial_state(),
            target_url="http://x",
            app_kind="hybrid",  # type: ignore[arg-type]
        )


def test_reconnaissance_refuses_when_not_loaded():
    s = _initial_state(loaded=False, log=["..."])
    with pytest.raises(ValueError, match="loaded is False"):
        reconnaissance(s, observations={"buttons": ["Submit"]})


def test_reconnaissance_records_when_loaded():
    s = _initial_state(loaded=True, log=["..."])
    out = reconnaissance(s, observations={"buttons": ["Submit"]})
    assert out["observations"]["buttons"] == ["Submit"]


def test_wait_for_load_false_yields_loop_prompt():
    s = _initial_state(log=["..."])
    out = wait_for_load(s, loaded=False, notes="long-polling")
    assert out["loaded"] is False
    assert "loaded=False" in out["current_prompt"]


def test_finalize_test_builds_assertion_summary():
    s = _initial_state(
        actions=[{"name": "click"}],
        assertions=[
            {"name": "a", "passed": True},
            {"name": "b", "passed": False},
            {"name": "c", "passed": True},
        ],
        log=["..."],
    )
    out = finalize_test(s, verdict="failed", notes="b failed")
    summary = out["summary"]
    assert summary["verdict"] == "failed"
    assert summary["assertions_total"] == 3
    assert summary["assertions_passed"] == 2
    assert summary["assertions_failed"] == 1


def test_finalize_test_rejects_invalid_verdict():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError):
        finalize_test(s, verdict="maybe")  # type: ignore[arg-type]


# == FSM walks via MCP ============================================


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_full_walk_passing():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_test", target_url="http://localhost:5173")
        await _step(client, "navigate")
        await _step(client, "wait_for_load", loaded=True)
        await _step(
            client,
            "reconnaissance",
            observations={"buttons": ["Log in"], "inputs": ["email", "password"]},
        )
        await _step(
            client,
            "identify_selectors",
            selectors={"click_login": "text=Log in", "fill_email": "input[name=email]"},
        )
        await _step(
            client,
            "execute_actions",
            actions=[
                {"name": "fill_email", "selector": "input[name=email]", "result": "ok"},
                {"name": "click_login", "selector": "text=Log in", "result": "ok"},
            ],
        )
        await _step(
            client,
            "verify",
            assertions=[
                {"name": "redirected_to_dashboard", "passed": True},
                {"name": "welcome_message", "passed": True},
            ],
        )
        out = _payload(await _step(client, "finalize_test", verdict="passed"))
    summary = out["state"]["summary"]
    assert summary["verdict"] == "passed"
    assert summary["assertions_passed"] == 2
    assert summary["assertions_failed"] == 0


@pytest.mark.asyncio
async def test_mcp_refuses_reconnaissance_before_wait_for_load():
    """Skipping wait_for_load (or reporting loaded=False) blocks
    reconnaissance via the action-body refusal."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_test", target_url="http://x")
        await _step(client, "navigate")
        await _step(client, "wait_for_load", loaded=False, notes="loader spinning")
        # `reconnaissance` is the valid next move per the transition
        # graph, but the action body refuses on loaded=False.
        r = await _step(
            client,
            "reconnaissance",
            observations={"buttons": []},
        )
        out = _payload(r)
        assert out.get("error") == "action_error"
        assert "loaded is False" in out.get("error_message", "")


@pytest.mark.asyncio
async def test_mcp_refuses_skipping_navigate():
    """Transition graph blocks calling wait_for_load before navigate."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_test", target_url="http://x")
        r = await _step(client, "wait_for_load", loaded=True)
        out = _payload(r)
        assert out.get("error") == "invalid_transition"
        assert "navigate" in out["valid_next_actions"]
