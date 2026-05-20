"""Streaming Burr action surfaced as MCP progress notifications.

Burr supports streaming actions via ``@streaming_action``: the
function yields intermediate chunks plus a final state. burrmcp
detects these and forwards each chunk to the client via
``ctx.report_progress``, the MCP-spec mechanism for partial results
during a long-running tool call. The final state arrives as the
regular tool response when the action completes.

This example narrates a short story chunk by chunk. In a real
system the chunks could be LLM tokens, retrieved documents,
streaming subprocess output, or anything else worth surfacing
incrementally.

Run as stdio:

    uv run python examples/streaming_narrate.py

Clients that don't supply a ``progressToken`` on their tool call
won't see the intermediate chunks; the action still runs and the
final result still arrives. Claude Code supports progress
notifications and will surface the chunks.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State
from burr.core.action import streaming_action

from burrmcp import ServingMode, mount


@streaming_action(reads=[], writes=["story"])
async def narrate(state: State, topic: str):
    """Stream a tiny story about ``topic``, chunk by chunk.

    Each ``yield (chunk, None)`` emits a partial update; the final
    ``yield (final_chunk, new_state)`` provides the resulting state.
    Burr handles assembling the stream; burrmcp forwards each
    intermediate chunk as a progress notification.
    """
    chunks = [
        f"Once upon a time, there was a {topic}.",
        f" The {topic} had a problem.",
        f" After much thought, the {topic} solved it.",
        " The end.",
    ]
    accumulated = ""
    for chunk in chunks:
        accumulated += chunk
        yield {"chunk": chunk, "accumulated_chars": len(accumulated)}, None
    yield {"chunk": "", "accumulated_chars": len(accumulated)}, state.update(story=accumulated)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(narrate=narrate)
        .with_state(story=None)
        .with_entrypoint("narrate")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="streaming-narrate",
        instructions=(
            "Streaming narration FSM. Calls to the narrate action "
            "emit chunks via MCP progress notifications during the "
            "call; the final result includes the assembled story in "
            "state['story']. The response also reports the chunk "
            "count for verification."
        ),
    )


if __name__ == "__main__":
    build_server().run()
