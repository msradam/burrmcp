"""Burr's prebuilt ``StateAndResultsFullLogger`` as a lifecycle hook.

Burr ships a default lifecycle hook,
``burr.lifecycle.StateAndResultsFullLogger``, that writes one JSONL
line per action with the post-step state, action name, result, and
wall-clock timing. It's a PreRunStepHook + PostRunStepHook pair
the same as any user-defined hook, so it plugs through
``mount()`` unchanged.

This demo wires it against a temp JSONL file per-session and
exposes the file contents at ``theodosia://full-log``. Useful as a
zero-config audit trail when you don't want a real tracker but do
want every action recorded.

Domain: a tiny counter FSM. ``tick()`` increments; ``reset()`` zeros
it out. Each call produces one JSONL row.

Run:

    uv run python examples/full_logger.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.lifecycle import StateAndResultsFullLogger

from theodosia import ServingMode, mount

# == actions =========================================================


@action(reads=["count"], writes=["count"])
async def tick(state: State) -> State:
    """Increment the counter.

    ``async`` so Burr's astep doesn't delegate to sync ``_step`` and
    fire ``post_run_step`` with stale pre-action state. See
    state_forking demo for the underlying Burr bug.
    """
    return state.update(count=state["count"] + 1)


@action(reads=[], writes=["count"])
async def reset(state: State) -> State:
    """Reset the counter to zero."""
    return state.update(count=0)


# == graph ===========================================================


def build_application(*, jsonl_path: str):
    logger = StateAndResultsFullLogger(jsonl_path=jsonl_path)
    return (
        ApplicationBuilder()
        .with_actions(tick=tick, reset=reset)
        .with_transitions(
            ("tick", "reset", Condition.expr("count > 0")),
            ("tick", "tick"),
            ("reset", "tick"),
        )
        .with_hooks(logger)
        .with_state(count=0)
        .with_entrypoint("tick")
        .build()
    )


def build_server():
    tmp_dir = Path(tempfile.mkdtemp(prefix="theodosia-full-logger-"))
    jsonl_path = tmp_dir / "full.jsonl"
    holder: dict[str, StateAndResultsFullLogger] = {}

    def factory():
        app = build_application(jsonl_path=str(jsonl_path))
        holder["logger"] = next(
            h for h in app._adapter_set.adapters if isinstance(h, StateAndResultsFullLogger)
        )
        return app

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="full-logger",
        instructions=(
            "Counter FSM (tick / reset) instrumented with Burr's "
            "prebuilt StateAndResultsFullLogger, writing one JSONL "
            "row per action to a per-session temp file. Read "
            "theodosia://full-log to see the captured rows."
        ),
    )

    @server.resource("theodosia://full-log")
    async def _full_log_resource() -> str:
        if "logger" in holder:
            holder["logger"].f.flush()
        if not jsonl_path.exists():
            return json.dumps({"path": str(jsonl_path), "rows": []})
        rows = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
        return json.dumps({"path": str(jsonl_path), "rows": rows}, indent=2, default=str)

    return server


if __name__ == "__main__":
    build_server().run()
