"""LLM-in-the-graph via MCP sampling (``ctx.sample``).

Counterpart to the local-model demos (``granite_oncall``,
``adaptive_crag``, ``mellea_qiskit_migration``, ``granite_guardian``).
Those need a specific model attached to the server (Ollama + Granite
or Mellea). This demo delegates the LLM call to the **caller's** model
via MCP's sampling protocol: when an action body calls
``ctx.sample(...)``, FastMCP sends a ``sampling/createMessage``
request back to the MCP client; the client's LLM does the inference;
the response arrives at the action body as ``result.text``.

Both patterns are legitimate. Use a local model when the FSM needs a
specific model (Terramind for geospatial, MedSigLIP for medical
imaging, fine-tuned domain models) that frontier general-purpose
models cannot substitute for. Use ``ctx.sample`` when the FSM's LLM
work is general-purpose and the connected agent already has a capable
model attached.

**Client compat (mid-2026):** Claude Code's headless CLI does NOT
support MCP server-to-client sampling; it returns the explicit error
"Client does not support sampling" and the action surfaces this as
``action_error``. MCP Inspector and ``fastmcp.Client`` with a
``sampling_handler`` work normally. The hermetic test suite uses the
latter; the smoke test asserts the Claude Code refusal as documented
compat.

The Burr-side glue: ``theodosia.current_mcp_context()`` returns the
FastMCP ``Context`` Theodosia's step handler threads into each action.
Action bodies pull it out and call any ``Context`` method. Outside an
action body the helper returns ``None``.

Domain: a tiny compose/revise loop.

* ``compose(topic, style="default")``: ask the caller's LLM to draft
  a short paragraph on ``topic`` in the requested style.
* ``revise(direction)``: ask the caller's LLM to refine the draft in
  the given direction (e.g. "more concise", "more technical").
  Loops; the agent picks when to stop.

State holds the current draft + a revision history. The agent walks
the FSM through MCP ``step`` calls; the server has no LLM attached.

Run:

    uv run python examples/caller_sample.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, current_mcp_context, mount

_TRACKER_PROJECT = "caller-sample-demo"


async def _sample_text(prompt: str, *, system: str | None = None, max_tokens: int = 300) -> str:
    """Sample text from the caller's LLM via ``ctx.sample``.

    Returns the response text. Raises ``RuntimeError`` if called outside
    a Theodosia-dispatched action body (no Context to delegate through).
    """
    ctx = current_mcp_context()
    if ctx is None:
        raise RuntimeError(
            "caller_sample actions need a FastMCP Context; call this FSM "
            "through theodosia.mount, not directly via app.astep."
        )
    result = await ctx.sample(messages=prompt, system_prompt=system, max_tokens=max_tokens)
    return result.text


@action(reads=[], writes=["draft", "topic", "style", "revisions"])
async def compose(state: State, topic: str, style: str = "default") -> State:
    """Draft a short paragraph on ``topic`` via ``ctx.sample``."""
    draft = await _sample_text(
        f"Write a short paragraph (3-4 sentences) about {topic}. "
        f"Style: {style}. Return only the paragraph, no preamble.",
        system="You are a careful writer. Be concise and concrete.",
    )
    return state.update(
        draft=draft,
        topic=topic,
        style=style,
        revisions=[],
    )


@action(reads=["draft", "topic", "revisions"], writes=["draft", "revisions"])
async def revise(state: State, direction: str) -> State:
    """Revise the draft in ``direction`` via ``ctx.sample``."""
    current = state["draft"] or ""
    if not current.strip():
        raise ValueError("nothing to revise; call compose first")
    revised = await _sample_text(
        f"Revise the following paragraph. Direction: {direction}.\n\n"
        f"Original:\n{current}\n\nReturn only the revised paragraph.",
        system="You are a careful editor. Preserve meaning while applying the direction.",
    )
    new_revisions = [*state["revisions"], {"direction": direction, "result": revised}]
    return state.update(draft=revised, revisions=new_revisions)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(compose=compose, revise=revise)
        .with_transitions(
            ("compose", "revise"),
            ("revise", "revise"),
        )
        .with_state(draft=None, topic=None, style=None, revisions=[])
        .with_entrypoint("compose")
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="caller-sample",
        instructions=(
            "Compose-and-revise FSM driven by the caller's LLM via "
            "MCP sampling. compose(topic, style) drafts; revise(direction) "
            "edits. The server has no LLM; each action calls ctx.sample "
            "to delegate to the connected agent's model. Loop revise "
            "until satisfied."
        ),
    )


if __name__ == "__main__":
    build_server().run()
