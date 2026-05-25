"""Tests for examples/streaming_hooks.py.

Validates @streaming_action.pydantic typing surface plus the three
streaming lifecycle hooks (PreStartStreamHook, PostStreamItemHook,
PostEndStreamHook) under both direct Burr drivers and the Theodosia
step path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from streaming_hooks import (
    Chunk,
    StreamStatsHook,
    TranscribeState,
    build_application,
    build_server,
)


@pytest.mark.asyncio
async def test_streaming_hooks_fire_via_astream_result():
    stats = StreamStatsHook()
    app = build_application(stats_hook=stats)
    state = app.state.update(prompt="hello there friend")
    app._state = state  # type: ignore[attr-defined]
    _, container = await app.astream_result(halt_after=["transcribe"])
    chunks = [chunk async for chunk in container]
    await container.get()
    assert stats.starts["transcribe"] == 1
    assert stats.items["transcribe"] == len(chunks)
    assert stats.ends["transcribe"] == 1
    assert len(stats.durations_ms["transcribe"]) == 1
    assert chunks  # produced something
    # Each chunk is validated against the Chunk pydantic model:
    for c in chunks:
        Chunk.model_validate(c)


@pytest.mark.asyncio
async def test_streaming_pydantic_writes_typed_state():
    stats = StreamStatsHook()
    app = build_application(stats_hook=stats)
    app._state = app.state.update(prompt="alpha beta gamma")  # type: ignore[attr-defined]
    _, container = await app.astream_result(halt_after=["transcribe"])
    async for _ in container:
        pass
    await container.get()
    typed = TranscribeState.model_validate(
        {k: app.state[k] for k in ("prompt", "transcript", "chunk_count")}
    )
    assert typed.transcript == "alpha beta gamma"
    assert typed.chunk_count == 3


@pytest.mark.asyncio
async def test_streaming_hooks_fire_through_mcp_step():
    """Theodosia routes streaming actions via app.astream_result inside
    step, so the streaming hooks fire when an MCP client drives the
    action via the step tool."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "transcribe", "inputs": {"prompt": "one two three four"}}
        )
        out = r.structured_content
        assert out.get("error") is None, out
        assert out["streamed"] is True
        # MCP-level chunk count matches what stats saw.
        text = (await client.read_resource("theodosia://stream-stats"))[0].text
        snap = json.loads(text)
        assert snap["transcribe"]["starts"] == 1
        assert snap["transcribe"]["ends"] == 1
        assert snap["transcribe"]["items"] == out["chunks"]
        assert len(snap["transcribe"]["durations_ms"]) == 1


@pytest.mark.asyncio
async def test_empty_prompt_still_streams_one_chunk():
    """Empty / whitespace prompt falls back to the '(silence)' chunk
    so the stream always produces at least one item."""
    stats = StreamStatsHook()
    app = build_application(stats_hook=stats)
    app._state = app.state.update(prompt="   ")  # type: ignore[attr-defined]
    _, container = await app.astream_result(halt_after=["transcribe"])
    chunks = [c async for c in container]
    await container.get()
    assert chunks
    assert stats.starts["transcribe"] == 1
    assert stats.ends["transcribe"] == 1
