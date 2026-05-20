"""Pydantic streaming action + Burr streaming hooks.

Pairs two upstream surfaces in one demo:

* ``@streaming_action.pydantic(...)``: a streaming action whose
  state slices, partial-chunk shape, and final state are all typed
  with Pydantic. Each yielded chunk is validated against
  ``stream_type``; the final state slice is validated against
  ``state_output_type``.
* ``PreStartStreamHook`` / ``PostStreamItemHook`` /
  ``PostEndStreamHook``: lifecycle hooks specific to streaming
  actions. ``pre_start_stream`` fires once when the generator is
  first kicked, ``post_stream_item`` fires per chunk, and
  ``post_end_stream`` fires once the generator is exhausted.

Both surfaces plug through ``mount()`` unchanged. BurrMCP drives
streaming actions via ``app.astream_result(halt_after=[action])``
inside the ``step`` tool, so the streaming hooks fire when an MCP
client calls ``step`` against a streaming action just as they would
for any other Burr driver.

Domain: a tiny "transcribe" action that emits a transcript of a
prompt chunk by chunk. The stream items are typed ``Chunk`` models;
the final state holds the assembled transcript plus the chunk count.

Run:

    uv run python examples/streaming_hooks.py
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

import pydantic
from burr.core import ApplicationBuilder
from burr.core.action import streaming_action
from burr.integrations.pydantic import PydanticTypingSystem
from burr.lifecycle.base import (
    PostEndStreamHook,
    PostStreamItemHook,
    PreStartStreamHook,
)
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "streaming-hooks-demo"


# == pydantic models =================================================


class TranscribeState(pydantic.BaseModel):
    """The state slice this action operates on."""

    prompt: str | None = None
    transcript: str | None = None
    chunk_count: int = 0


class Chunk(pydantic.BaseModel):
    """The shape of each streamed partial."""

    index: int
    text: str
    cumulative_chars: int


# == streaming hooks =================================================


class StreamStatsHook(PreStartStreamHook, PostStreamItemHook, PostEndStreamHook):
    """Counts streaming events per action and records wall-clock
    duration from stream start to stream end.
    """

    def __init__(self) -> None:
        self.starts: dict[str, int] = defaultdict(int)
        self.items: dict[str, int] = defaultdict(int)
        self.ends: dict[str, int] = defaultdict(int)
        self.durations_ms: dict[str, list[float]] = defaultdict(list)
        self._open: dict[tuple[str, int], float] = {}

    def pre_start_stream(
        self,
        *,
        action: str,
        sequence_id: int,
        app_id: str,
        partition_key: str | None,
        **_: Any,
    ) -> None:
        self.starts[action] += 1
        self._open[(app_id, sequence_id)] = time.perf_counter()

    def post_stream_item(
        self,
        *,
        item: Any,
        item_index: int,
        action: str,
        sequence_id: int,
        app_id: str,
        partition_key: str | None,
        **_: Any,
    ) -> None:
        self.items[action] += 1

    def post_end_stream(
        self,
        *,
        action: str,
        sequence_id: int,
        app_id: str,
        partition_key: str | None,
        **_: Any,
    ) -> None:
        self.ends[action] += 1
        start = self._open.pop((app_id, sequence_id), None)
        if start is not None:
            self.durations_ms[action].append((time.perf_counter() - start) * 1000)

    def snapshot(self) -> dict[str, Any]:
        actions = set(self.starts) | set(self.items) | set(self.ends)
        return {
            action_name: {
                "starts": self.starts.get(action_name, 0),
                "items": self.items.get(action_name, 0),
                "ends": self.ends.get(action_name, 0),
                "durations_ms": [round(ms, 3) for ms in self.durations_ms.get(action_name, [])],
            }
            for action_name in sorted(actions)
        }


# == streaming action ================================================


@streaming_action.pydantic(
    reads=["prompt"],
    writes=["transcript", "chunk_count"],
    state_input_type=TranscribeState,
    state_output_type=TranscribeState,
    stream_type=Chunk,
)
async def transcribe(state: TranscribeState):
    """Stream a chunked transcript of ``state.prompt``.

    Each ``yield (Chunk, None)`` emits a partial. The terminating
    yield carries a ``TranscribeState`` with the writes set; Burr
    validates this against ``state_output_type``.
    """
    prompt = state.prompt or ""
    pieces = [piece for piece in prompt.split() if piece] or ["(silence)"]
    accumulated = ""
    for idx, piece in enumerate(pieces):
        text = piece if idx == 0 else f" {piece}"
        accumulated += text
        yield Chunk(index=idx, text=text, cumulative_chars=len(accumulated)), None
    yield (
        Chunk(index=len(pieces), text="", cumulative_chars=len(accumulated)),
        TranscribeState(
            prompt=prompt,
            transcript=accumulated,
            chunk_count=len(pieces),
        ),
    )


# == graph ===========================================================


def build_application(*, stats_hook: StreamStatsHook | None = None):
    initial = TranscribeState()
    builder = (
        ApplicationBuilder()
        .with_typing(PydanticTypingSystem(TranscribeState))
        .with_actions(transcribe=transcribe)
        .with_state(**initial.model_dump())
        .with_entrypoint("transcribe")
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
    )
    if stats_hook is not None:
        builder = builder.with_hooks(stats_hook)
    return builder.build()


def build_server():
    stats = StreamStatsHook()

    def factory():
        return build_application(stats_hook=stats)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="streaming-hooks",
        instructions=(
            "Streaming pydantic action 'transcribe(prompt: str)' that "
            "yields validated Chunk partials and a typed final state. "
            "PreStartStreamHook / PostStreamItemHook / PostEndStreamHook "
            "are wired; their counters and wall-clock durations are at "
            "burr://stream-stats. Chunks also surface via MCP progress "
            "notifications."
        ),
    )

    @server.resource("burr://stream-stats")
    async def _stream_stats_resource() -> str:
        return json.dumps(stats.snapshot(), indent=2)

    return server


if __name__ == "__main__":
    build_server().run()
