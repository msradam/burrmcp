"""Tests for examples/local_shell.py (burr-shell).

Exercises real subprocess execution against a per-test sandbox. Each
test gets an explicit ``sandbox`` path so we don't pollute /tmp with
session-on-process-exit leakage.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from local_shell import (  # noqa: E402
    _DATA_DIR,
    build_application,
    build_server,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A fresh sandbox seeded with the shipped sample data, per test."""
    sb = tmp_path / "sandbox"
    sb.mkdir()
    for child in _DATA_DIR.iterdir():
        if child.is_dir():
            shutil.copytree(child, sb / child.name)
        else:
            shutil.copy2(child, sb / child.name)
    return sb


async def _aforce_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        await app.astep(inputs=inputs or None)
    finally:
        app.get_next_action = original


@pytest.mark.asyncio
async def test_execute_lists_shipped_sandbox_contents(sandbox):
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="ls -1")
    entry = app.state["history"][-1]
    assert entry["exit_code"] == 0
    names = set(entry["stdout"].split())
    assert {"data.csv", "config.yaml", "notes"}.issubset(names)


@pytest.mark.asyncio
async def test_execute_real_command_against_real_data(sandbox):
    """wc -l on the shipped CSV returns the real line count."""
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="wc -l data.csv")
    entry = app.state["history"][-1]
    assert entry["exit_code"] == 0
    # The shipped data.csv is 8 lines (1 header + 7 events).
    assert entry["stdout"].split()[0] == "8"


@pytest.mark.asyncio
async def test_write_then_read_round_trip(sandbox):
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="echo 'hello world' > notes/today.md")
    await _aforce_step(app, "execute", command="cat notes/today.md")
    last = app.state["history"][-1]
    assert last["stdout"].strip() == "hello world"
    # And the file actually exists on the sandbox FS.
    assert (sandbox / "notes" / "today.md").read_text().strip() == "hello world"


@pytest.mark.asyncio
async def test_shipped_data_is_unaffected_by_sandbox_writes(sandbox):
    """Writing in the sandbox does NOT touch examples/data/local_shell/."""
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="echo overwritten > config.yaml")
    sandbox_text = (sandbox / "config.yaml").read_text()
    shipped_text = (_DATA_DIR / "config.yaml").read_text()
    assert sandbox_text.strip() == "overwritten"
    assert "overwritten" not in shipped_text
    assert "service: api-gateway" in shipped_text


@pytest.mark.asyncio
async def test_absolute_path_is_refused(sandbox):
    app = build_application(sandbox=sandbox)
    with pytest.raises(ValueError, match="sandbox-escape pattern"):
        await _aforce_step(app, "execute", command="cat /etc/passwd")


@pytest.mark.asyncio
async def test_parent_traversal_is_refused(sandbox):
    app = build_application(sandbox=sandbox)
    with pytest.raises(ValueError, match="sandbox-escape pattern"):
        await _aforce_step(app, "execute", command="ls ../")


@pytest.mark.asyncio
async def test_subshell_is_refused(sandbox):
    app = build_application(sandbox=sandbox)
    with pytest.raises(ValueError, match="sandbox-escape pattern"):
        await _aforce_step(app, "execute", command="echo $(cat data.csv)")


@pytest.mark.asyncio
async def test_pipe_to_shell_is_refused(sandbox):
    app = build_application(sandbox=sandbox)
    with pytest.raises(ValueError, match="sandbox-escape pattern"):
        await _aforce_step(app, "execute", command="cat data.csv | sh")


@pytest.mark.asyncio
async def test_empty_command_is_refused(sandbox):
    app = build_application(sandbox=sandbox)
    with pytest.raises(ValueError, match="command must not be empty"):
        await _aforce_step(app, "execute", command="   ")


@pytest.mark.asyncio
async def test_command_timeout_recorded_in_history(sandbox):
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="sleep 5", timeout_seconds=1)
    entry = app.state["history"][-1]
    assert entry.get("timed_out") is True
    assert entry["exit_code"] is None


@pytest.mark.asyncio
async def test_failed_command_is_still_recorded(sandbox):
    """Non-zero exit codes record fully; the FSM keeps going."""
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="cat does-not-exist.txt")
    entry = app.state["history"][-1]
    assert entry["exit_code"] != 0
    assert entry["stderr"]


@pytest.mark.asyncio
async def test_done_summarises(sandbox):
    app = build_application(sandbox=sandbox)
    await _aforce_step(app, "execute", command="ls")
    await _aforce_step(app, "execute", command="cat does-not-exist.txt")
    await _aforce_step(app, "done")
    summary = app.state["summary"]
    assert summary["command_count"] == 2
    assert summary["successful_count"] == 1
    assert summary["failed_count"] == 1


@pytest.mark.asyncio
async def test_full_walk_through_mcp_step():
    """End-to-end through MCP; server uses an auto-prepared sandbox."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "execute", "inputs": {"command": "ls -1"}})
        out = json.loads(r.content[0].text)
        assert out.get("error") is None, out
        assert "data.csv" in out["state"]["history"][-1]["stdout"]

        r = await client.call_tool(
            "step",
            {"action": "execute", "inputs": {"command": "cat /etc/passwd"}},
        )
        refusal = json.loads(r.content[0].text)
        assert refusal["error"] == "action_error"
        assert "sandbox-escape" in refusal["error_message"]

        r = await client.call_tool("step", {"action": "done", "inputs": {}})
        done = json.loads(r.content[0].text)
        # The refused call wasn't appended to state.history, so the
        # summary only sees the successful ls.
        assert done["state"]["summary"]["command_count"] == 1
        assert done["valid_next_actions"] == []
