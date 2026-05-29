"""Burr streaming actions surface chunks as MCP progress notifications.

A ``@streaming_action.pydantic`` yields typed intermediate chunks then a
final state. Theodosia forwards each chunk via ``ctx.report_progress``
(best-effort) and returns the final state in the same response envelope
as a regular ``step``, with ``streamed: True`` and ``chunks: N`` on the
payload.

This is the streaming-typed path; the simpler non-pydantic
``streaming_action`` decorator exercises the same code path in
``examples/streaming_narrate.py``.
"""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder
from burr.core.action import streaming_action
from burr.integrations.pydantic import PydanticTypingSystem
from fastmcp import Client
from pydantic import BaseModel

from theodosia import mount


class Chunk(BaseModel):
    """One streaming partial."""

    n: int


class _State(BaseModel):
    count: int = 0


@streaming_action.pydantic(
    reads=[], writes=["count"], state_input_type=_State, state_output_type=_State, stream_type=Chunk
)
async def _stream(state: _State):
    """Yield three partials, then update final state."""
    last = Chunk(n=0)
    for i in range(3):
        last = Chunk(n=i)
        yield last, None
    yield last, _State(count=3)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(stream=_stream)
        .with_typing(PydanticTypingSystem(_State))
        .with_state(_State(count=0))
        .with_entrypoint("stream")
        .build()
    )


@pytest.mark.asyncio
async def test_streaming_action_returns_with_chunk_count():
    server = mount(_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool("step", {"action": "stream", "inputs": {}})
    out = r.structured_content or {}
    assert out.get("error") is None, out
    assert out.get("streamed") is True
    assert out.get("chunks", 0) >= 1


@pytest.mark.asyncio
async def test_streaming_action_final_state_lands():
    """The final state from a streaming action lands in the FSM correctly."""
    server = mount(_factory, name="t")
    async with Client(server) as c:
        r = await c.call_tool("step", {"action": "stream", "inputs": {}})
    state = (r.structured_content or {}).get("state", {})
    assert state.get("count") == 3
