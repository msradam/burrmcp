"""Interactive user confirmation via MCP elicitation (``ctx.elicit``).

Burr's transition graph refuses invalid actions structurally and the
``valid_next_actions`` payload tells the agent how to recover. That
handles agent-side mistakes. ``ctx.elicit`` adds the other half: the
server can interrupt mid-action to ask the **user** a question,
producing a real human-in-the-loop confirmation gate. The action
body awaits the user's response (or decline / cancel) and decides
what to do.

Pattern: safety-rail FSM where destructive steps require confirmation.

Domain: a tiny "purge" workflow.

* ``stage(item)``: add an item to the staging list. Repeatable.
* ``purge()``: ask the user via ``ctx.elicit`` to confirm or abort.
  * On confirm: move staged items to ``purged``, clear staging.
  * On decline / cancel: clear staging without purging; mark as
    ``aborted``.

The agent walks the FSM; the user's response gates the destructive
step. No agent-side prompt engineering can bypass the gate because the
elicitation happens server-side, in the action body, before any state
mutation that depends on the user's choice.

Run:

    uv run python examples/elicit_confirm.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient
from fastmcp.server.elicitation import AcceptedElicitation

from burrmcp import ServingMode, current_mcp_context, mount

_TRACKER_PROJECT = "elicit-confirm-demo"


@action(reads=["staged"], writes=["staged"])
async def stage(state: State, item: str) -> State:
    """Add an item to the staging list."""
    if not item.strip():
        raise ValueError("item must not be empty")
    return state.update(staged=[*state["staged"], item])


@action(reads=["staged", "purged"], writes=["staged", "purged", "outcome"])
async def purge(state: State) -> State:
    """Ask the user to confirm via ``ctx.elicit``, then act on the answer.

    Calls ``ctx.elicit(message, list[str])`` with ``["confirm", "abort"]``;
    the user picks one. On confirm: staged items move to ``purged``. On
    decline / cancel / abort: staging clears with no purge.
    """
    ctx = current_mcp_context()
    if ctx is None:
        raise RuntimeError("purge needs a FastMCP Context; call this FSM through burrmcp.mount.")
    staged = list(state["staged"])
    if not staged:
        return state.update(outcome="nothing_staged")
    message = (
        f"About to purge {len(staged)} item(s): {staged}. "
        "Choose 'confirm' to delete, 'abort' to keep."
    )
    elicit_result = await ctx.elicit(message, ["confirm", "abort"])
    if isinstance(elicit_result, AcceptedElicitation) and elicit_result.data == "confirm":
        return state.update(
            staged=[],
            purged=[*state["purged"], *staged],
            outcome="purged",
        )
    return state.update(staged=[], outcome="aborted")


def build_application():
    can_stage = Condition.expr("outcome is None")
    return (
        ApplicationBuilder()
        .with_actions(stage=stage, purge=purge)
        .with_transitions(
            ("stage", "stage", can_stage),
            ("stage", "purge", can_stage),
        )
        .with_state(staged=[], purged=[], outcome=None)
        .with_entrypoint("stage")
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="elicit-confirm",
        instructions=(
            "Stage-and-purge FSM with user-confirmation gating. "
            "stage(item) queues items; purge() asks the user via "
            "ctx.elicit to confirm or abort. The user's choice gates "
            "the destructive step; no agent recovery can bypass it."
        ),
    )


if __name__ == "__main__":
    build_server().run()
