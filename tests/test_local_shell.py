"""Local-shell FSM: a Claude-Code-style safety-rails demo.

Tests cover the three FSM-enforced safety rules: read-before-edit
(action-level refusal), test-before-commit (transition-level refusal),
and request-before-confirm for deletion. Also covers the happy path
and that editing invalidates the prior test result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from local_shell import build_server  # noqa: E402


async def _start(client):
    """Every session must start at the ``list_files`` entrypoint
    before transitions out of it become reachable."""
    await client.call_tool("step", {"action": "list_files", "inputs": {}})


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_happy_path_read_edit_test_commit():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="main.py")
        await _step(client, "edit_file", path="main.py", new_content='print("goodbye")\n')
        await _step(client, "run_tests", result="passed")
        out = _payload(await _step(client, "commit", message="say goodbye"))
        state = out["state"]
        assert state["workspace"]["main.py"] == 'print("goodbye")\n'
        assert state["pending_edits"] == {}
        assert len(state["commits"]) == 1
        assert state["commits"][0]["message"] == "say goodbye"


@pytest.mark.asyncio
async def test_edit_without_read_is_refused_with_action_error():
    """The headline safety rule: cannot edit a file you haven't read."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "edit_file", path="main.py", new_content="x = 1\n"))
        assert out["error"] == "action_error"
        assert "must read" in out["error_message"]
        assert "main.py" in out["error_message"]


@pytest.mark.asyncio
async def test_reading_one_file_does_not_unlock_editing_another():
    """The read-tracking is per-path, not a global 'has-read-something' flag."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="main.py")
        out = _payload(await _step(client, "edit_file", path="utils.py", new_content="x = 1\n"))
        assert out["error"] == "action_error"
        assert "utils.py" in out["error_message"]


@pytest.mark.asyncio
async def test_commit_without_tests_is_refused_with_invalid_transition():
    """Transition-level gate: pending edits + last_test_result==passed."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="main.py")
        await _step(client, "edit_file", path="main.py", new_content="x = 1\n")
        # No run_tests call. Commit must be refused.
        out = _payload(await _step(client, "commit", message="wip"))
        assert out["error"] == "invalid_transition"
        assert "run_tests" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_failing_tests_blocks_commit():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="main.py")
        await _step(client, "edit_file", path="main.py", new_content="syntax error\n")
        await _step(client, "run_tests", result="failed")
        out = _payload(await _step(client, "commit", message="broken"))
        assert out["error"] == "invalid_transition"


@pytest.mark.asyncio
async def test_edit_after_tests_invalidates_prior_result():
    """An edit_file call sets last_test_result=unknown so the agent
    has to run tests *again* before committing."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="main.py")
        await _step(client, "edit_file", path="main.py", new_content="a = 1\n")
        await _step(client, "run_tests", result="passed")
        # Second edit invalidates the prior pass.
        await _step(client, "edit_file", path="main.py", new_content="a = 2\n")
        out = _payload(await _step(client, "commit", message="second edit"))
        assert out["error"] == "invalid_transition"


@pytest.mark.asyncio
async def test_confirm_delete_without_request_is_refused():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "confirm_delete"))
        assert out["error"] == "invalid_transition"
        assert "confirm_delete" not in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_two_step_delete_flow():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "request_delete", path="README.md")
        out = _payload(await _step(client, "confirm_delete"))
        assert "README.md" not in out["state"]["workspace"]
        assert out["state"]["pending_delete"] is None


@pytest.mark.asyncio
async def test_commit_clears_pending_and_resets_test_result():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "read_file", path="utils.py")
        await _step(client, "edit_file", path="utils.py", new_content="x = 99\n")
        await _step(client, "run_tests", result="passed")
        out = _payload(await _step(client, "commit", message="bump"))
        assert out["state"]["pending_edits"] == {}
        assert out["state"]["last_test_result"] == "unknown"
        # Commit is no longer a valid next action with no fresh edits.
        assert "commit" not in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_burr_next_advertises_legal_actions_only():
    """A fresh session has no pending edits and no pending delete, so
    commit and confirm_delete should be absent from burr://next."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        nxt = json.loads((await client.read_resource("burr://next"))[0].text)
        assert "commit" not in nxt
        assert "confirm_delete" not in nxt
        # But the safe actions are there.
        assert "read_file" in nxt
        assert "list_files" in nxt
        assert "run_tests" in nxt
