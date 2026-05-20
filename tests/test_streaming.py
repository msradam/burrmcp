"""Streaming Burr actions surface chunks as MCP progress notifications.

Confirms the streaming code path runs end-to-end: the action's
intermediate chunks are iterated, the final state arrives in the
tool response, the chunk count is reported, and ``streamed: True``
flags the response shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from streaming_narrate import build_server


@pytest.mark.asyncio
async def test_streaming_action_returns_final_state():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "narrate", "inputs": {"topic": "wizard"}})
        out = json.loads(r.content[0].text)
        assert out["state"]["story"] is not None
        assert "wizard" in out["state"]["story"]
        assert out["streamed"] is True
        # Burr's streaming protocol consumes the final state-bearing
        # yield via container.get(); only the intermediate chunks
        # surface through the async iterator. The narrate action has
        # four intermediate chunks plus the final state, so four
        # chunks reach the progress handler.
        assert out["chunks"] == 4


@pytest.mark.asyncio
async def test_streaming_action_records_in_history():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "narrate", "inputs": {"topic": "explorer"}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1
        entry = history[0]
        assert entry["action"] == "narrate"
        assert entry["refused"] is False
        assert "explorer" in entry["state_after"]["story"]


@pytest.mark.asyncio
async def test_streaming_action_progress_notifications_received():
    """When the client supplies a progress handler, every chunk fires
    a progress notification. We capture them with an in-process
    handler and verify chunk count + ordering."""
    progress_events: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        progress_events.append((progress, message))

    server = build_server()
    async with Client(server, progress_handler=on_progress) as client:
        await client.call_tool("step", {"action": "narrate", "inputs": {"topic": "knight"}})

    # Four intermediate chunks => four progress events.
    assert len(progress_events) == 4
    progress_values = [p for p, _ in progress_events]
    assert progress_values == [1.0, 2.0, 3.0, 4.0]
    # Each message is the JSON-serialised chunk.
    first_msg = json.loads(progress_events[0][1])
    assert "chunk" in first_msg
    assert "knight" in first_msg["chunk"]
