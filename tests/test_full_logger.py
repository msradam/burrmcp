"""Tests for examples/full_logger.py."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from full_logger import build_application, build_server  # noqa: E402


async def _aforce_step(app, action_name: str):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        await app.astep()
    finally:
        app.get_next_action = original


def _read_jsonl_rows(jsonl_path: Path, *, logger_handle) -> list[dict]:
    """StateAndResultsFullLogger buffers writes; flush before reading."""
    logger_handle.f.flush()
    return [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_full_logger_writes_one_row_per_action(tmp_path):
    jsonl_path = tmp_path / "out.jsonl"
    app = build_application(jsonl_path=str(jsonl_path))
    logger_handle = next(
        h for h in app._adapter_set.adapters if h.__class__.__name__ == "StateAndResultsFullLogger"
    )
    await _aforce_step(app, "tick")
    await _aforce_step(app, "tick")
    await _aforce_step(app, "reset")
    rows = _read_jsonl_rows(jsonl_path, logger_handle=logger_handle)
    assert [r["action"] for r in rows] == ["tick", "tick", "reset"]
    assert [r["state"]["count"] for r in rows] == [1, 2, 0]


def test_full_logger_rejects_non_jsonl_path():
    with tempfile.TemporaryDirectory() as tmp:
        bad_path = str(Path(tmp) / "out.log")
        with pytest.raises(ValueError, match=r"jsonl_path must end with \.jsonl"):
            build_application(jsonl_path=bad_path)


@pytest.mark.asyncio
async def test_full_log_resource_via_mcp():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        text = (await client.read_resource("burr://full-log"))[0].text
        payload = json.loads(text)
        assert len(payload["rows"]) == 2
        assert payload["rows"][0]["action"] == "tick"
        assert payload["rows"][0]["state"]["count"] == 1
        assert payload["rows"][1]["state"]["count"] == 2
