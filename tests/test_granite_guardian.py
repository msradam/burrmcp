"""Tests for examples/granite_guardian.py.

Hermetic via the monkey-patchable ``_call_guardian`` indirection; no
Ollama call ever leaves the test process.

Coverage:
* start input validation (empty description, max_attempts < 1)
* propose_call records into history
* check_safety stores the verdict from _call_guardian
* The retry-as-transitions branch: safe -> finalize_safe; unsafe with
  retries -> propose_call (loop); unsafe with no retries ->
  finalize_refused.
* End-to-end walks for both terminal outcomes via mount() + Client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

import granite_guardian as guardian_module
from granite_guardian import build_server, start


def _initial_state(**overrides):
    from burr.core.state import State

    base = {
        "operation_description": "",
        "max_attempts": 3,
        "proposed_calls": [],
        "verdicts": [],
        "approved_call": None,
        "final_outcome": None,
        "current_prompt": "",
        "log": [],
    }
    base.update(overrides)
    return State(base)


# == unit tests ===================================================


def test_start_rejects_empty_description():
    with pytest.raises(ValueError):
        start(_initial_state(), operation_description="  ", max_attempts=3)


def test_start_rejects_zero_or_negative_max_attempts():
    with pytest.raises(ValueError):
        start(_initial_state(), operation_description="x", max_attempts=0)


def test_start_initialises_state_cleanly():
    out = start(
        _initial_state(),
        operation_description="delete files",
        max_attempts=5,
    )
    assert out["operation_description"] == "delete files"
    assert out["max_attempts"] == 5
    assert out["proposed_calls"] == []
    assert out["verdicts"] == []


def _patch_guardian(monkeypatch, verdicts):
    """Monkey-patch _call_guardian to return verdicts in sequence.

    ``verdicts`` is a list of ("safe" | "unsafe", reason) tuples.
    Each call to _call_guardian consumes the next one.
    """
    iterator = iter(verdicts)

    async def fake(*, operation, args, model=None):
        try:
            verdict, reason = next(iterator)
        except StopIteration as exc:
            raise AssertionError("fake _call_guardian exhausted: more calls than mocks") from exc
        return {"verdict": verdict, "reason": reason, "raw": f"{verdict}\n{reason}"}

    monkeypatch.setattr(guardian_module, "_call_guardian", fake)


# == FSM walks via MCP Client (which drives `astep` internally) ==


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_safe_on_first_proposal_walks_to_finalize_safe(monkeypatch):
    _patch_guardian(monkeypatch, [("safe", "low-risk operation")])
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start", operation_description="read tmp file")
        await _step(client, "propose_call", operation="read_file", args={"path": "/tmp/x.txt"})
        await _step(client, "check_safety")
        out = _payload(await _step(client, "finalize_safe"))
    assert out["state"]["final_outcome"] == "approved"
    assert out["state"]["approved_call"]["operation"] == "read_file"
    assert len(out["state"]["verdicts"]) == 1
    assert out["state"]["verdicts"][0]["verdict"] == "safe"


@pytest.mark.asyncio
async def test_one_unsafe_then_safe_loops_through_propose_call(monkeypatch):
    _patch_guardian(
        monkeypatch,
        [
            ("unsafe", "/etc/passwd is system-critical"),
            ("safe", "/tmp scratch is fine"),
        ],
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start", operation_description="delete files")
        await _step(client, "propose_call", operation="delete_file", args={"path": "/etc/passwd"})
        await _step(client, "check_safety")
        # Guardian said unsafe, retries remain: agent revises.
        await _step(
            client, "propose_call", operation="delete_file", args={"path": "/tmp/scratch.txt"}
        )
        await _step(client, "check_safety")
        out = _payload(await _step(client, "finalize_safe"))
    assert out["state"]["final_outcome"] == "approved"
    assert out["state"]["approved_call"]["args"] == {"path": "/tmp/scratch.txt"}
    assert [v["verdict"] for v in out["state"]["verdicts"]] == ["unsafe", "safe"]


@pytest.mark.asyncio
async def test_max_attempts_unsafe_routes_to_finalize_refused(monkeypatch):
    _patch_guardian(
        monkeypatch,
        [("unsafe", "first"), ("unsafe", "second"), ("unsafe", "third")],
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start", operation_description="dangerous op", max_attempts=3)
        for i in range(3):
            await _step(client, "propose_call", operation=f"op_{i}", args={"i": i})
            await _step(client, "check_safety")
        out = _payload(await _step(client, "finalize_refused"))
    assert out["state"]["final_outcome"] == "refused"
    assert len(out["state"]["verdicts"]) == 3
    assert all(v["verdict"] == "unsafe" for v in out["state"]["verdicts"])


# == MCP wire-shape tests ========================================


@pytest.mark.asyncio
async def test_refuses_finalize_safe_after_unsafe_verdict(monkeypatch):
    """After Guardian classifies unsafe, finalize_safe should not be a
    valid next action; the agent must either revise (propose_call) or
    exhaust attempts."""
    _patch_guardian(monkeypatch, [("unsafe", "no")])
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start", operation_description="x")
        await _step(client, "propose_call", operation="rm", args={"path": "/"})
        out = _payload(await _step(client, "check_safety"))
        assert out["state"]["verdicts"][-1]["verdict"] == "unsafe"
        assert "finalize_safe" not in out["valid_next_actions"]
        # propose_call should be valid (retries remain at the default 3).
        assert "propose_call" in out["valid_next_actions"]
