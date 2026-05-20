"""Hardening: action errors, concurrency, and non-JSON state.

Three things a frontier-model deployment will hit that the earlier
tests didn't cover:

1. An action raising an exception. The adapter catches it, records a
   structured ``refused=True, refusal_reason="action_error"`` entry,
   and returns a clean error to the client.
2. Concurrent step calls within one session. Burr Applications are
   not thread-safe; the per-session lock should serialise them.
3. State values that aren't JSON-serialisable. The state resource
   should coerce them to strings and surface which keys were coerced.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from burrmcp import ServingMode, mount

# ── action errors ────────────────────────────────────────────────────


@action(reads=[], writes=["stage"])
def boom(state: State) -> State:
    """An action that raises during execution."""
    raise RuntimeError("intentional failure for tests")


@action(reads=[], writes=["stage"])
def ok(state: State) -> State:
    return state.update(stage="ok")


def _erroring_app():
    return (
        ApplicationBuilder()
        .with_actions(boom=boom, ok=ok)
        .with_transitions(("boom", "ok"))
        .with_state(stage="new")
        .with_entrypoint("boom")
        .build()
    )


@pytest.mark.asyncio
async def test_action_error_returns_structured_response_and_records_history():
    server = mount(_erroring_app, mode=ServingMode.STEP, name="boom")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "boom", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert out["error_type"] == "RuntimeError"
        assert "intentional failure" in out["error_message"]

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert len(history) == 1
        entry = history[0]
        assert entry["refused"] is True
        assert entry["refusal_reason"] == "action_error"
        assert entry["error_type"] == "RuntimeError"
        assert "intentional failure" in entry["error_message"]
        assert entry["state_after"] is None


@pytest.mark.asyncio
async def test_action_error_doesnt_advance_state():
    """A raised action shouldn't move the FSM forward."""
    server = mount(_erroring_app, mode=ServingMode.STEP, name="boom2")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "boom", "inputs": {}})
        # FSM should still be at the entrypoint.
        next_actions = json.loads((await client.read_resource("burr://next"))[0].text)
        assert next_actions == ["boom"]


# ── concurrent step calls within one session ────────────────────────


@action(reads=["n"], writes=["n"])
async def increment(state: State) -> State:
    """Read n, sleep briefly to encourage interleaving, write n+1."""
    n = state.get("n", 0)
    await asyncio.sleep(0.01)
    return state.update(n=n + 1)


def _counter_app():
    return (
        ApplicationBuilder()
        .with_actions(increment=increment)
        .with_transitions(("increment", "increment"))  # self-loop
        .with_state(n=0)
        .with_entrypoint("increment")
        .build()
    )


@pytest.mark.asyncio
async def test_concurrent_steps_in_one_session_are_serialised():
    """Without the lock, parallel increments would race and lose updates."""
    server = mount(_counter_app, mode=ServingMode.STEP, name="counter")
    async with Client(server) as client:
        # Fire ten increments in parallel from the same session.
        await asyncio.gather(
            *(client.call_tool("step", {"action": "increment", "inputs": {}}) for _ in range(10))
        )
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["n"] == 10, f"lost increments; saw n={state['n']} expected 10"


# ── non-JSON state coercion ──────────────────────────────────────────


class _NotJsonable:
    def __repr__(self) -> str:
        return "<NotJsonable id=42>"


@action(reads=[], writes=["pipe", "ok"])
def stash_object(state: State) -> State:
    """Put a non-JSON-serialisable object into state."""
    return state.update(pipe=_NotJsonable(), ok="yes")


def _odd_state_app():
    return (
        ApplicationBuilder()
        .with_actions(stash_object=stash_object)
        .with_state()
        .with_entrypoint("stash_object")
        .build()
    )


@pytest.mark.asyncio
async def test_non_json_state_is_coerced_and_keys_surfaced():
    server = mount(_odd_state_app, mode=ServingMode.STEP, name="odd")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "stash_object", "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        # JSON-friendly value passed through unchanged.
        assert state["ok"] == "yes"
        # Non-JSON value was stringified.
        assert state["pipe"] == "<NotJsonable id=42>"
        # And the client was told.
        assert state["_burrmcp"]["coerced_keys"] == ["pipe"]


@pytest.mark.asyncio
async def test_clean_state_has_no_coercion_marker():
    """Without coercion, no _burrmcp marker shows up."""
    from coffee_order import build_application

    server = mount(build_application, mode=ServingMode.STEP, name="clean")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert "_burrmcp" not in state
