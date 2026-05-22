"""reset_session: the escape hatch from terminal states.

Surfaces an MCP tool that, in factory mode, rebuilds the session's
Application from the factory, clears sub-runs, and appends a marker
to history without wiping prior entries. In shared-app mode the tool
refuses because resetting one client's view would affect everyone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from burrmcp import ServingMode, mount

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from adventure import build_application as adventure_factory


@pytest.mark.asyncio
async def test_reset_session_appears_as_a_tool():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="adv-reset")
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "reset_session" in names


@pytest.mark.asyncio
async def test_reset_from_terminal_restores_entrypoint():
    """Walk the adventure to victory, then reset. The next valid action
    should be the entrypoint again."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="adv-reset-walk")
    async with Client(server) as client:
        for a in [
            "enter_foyer",
            "go_to_library",
            "take_key",
            "go_to_hallway",
            "unlock_door",
            "go_to_garden_via_hallway",
        ]:
            await client.call_tool("step", {"action": a, "inputs": {}})
        # Terminal: valid_next is empty.
        valid_at_terminal = json.loads((await client.read_resource("burr://next"))[0].text)
        assert valid_at_terminal == []

        # Reset.
        r = await client.call_tool("reset_session", {})
        out = r.structured_content
        assert out["action"] == "reset_session"
        # The previous state captured the win.
        assert out["result"]["previous_state"]["won"] is True
        # The new state is fresh.
        assert out["state"].get("won") is False
        assert out["valid_next_actions"] == ["enter_foyer"]


@pytest.mark.asyncio
async def test_reset_session_preserves_prior_history():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="adv-reset-history")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("step", {"action": "go_to_library", "inputs": {}})
        await client.call_tool("reset_session", {})
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions_in_order = [h["action"] for h in history]
        assert actions_in_order == [
            "enter_foyer",
            "go_to_library",
            "reset_session",
            "enter_foyer",
        ]
        # The reset entry is not a refusal.
        reset_entry = history[2]
        assert reset_entry["refused"] is False
        # Subsequent action's state_after should reflect the fresh start.
        assert history[3]["state_after"]["room"] == "foyer"


@pytest.mark.asyncio
async def test_reset_session_clears_subruns():
    """A session that spawned sub-runs gets them wiped on reset."""
    from subgraphs import build_application as sub_factory

    server = mount(sub_factory, mode=ServingMode.STEP, name="sub-reset")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "investigate", "inputs": {"source": "x"}})
        idx_before = json.loads((await client.read_resource("burr://subruns"))[0].text)
        assert len(idx_before) == 1

        await client.call_tool("reset_session", {})
        idx_after = json.loads((await client.read_resource("burr://subruns"))[0].text)
        assert idx_after == []


@pytest.mark.asyncio
async def test_reset_session_refuses_in_shared_app_mode():
    """Shared-app mode means an Application instance was passed to
    mount. Resetting it would affect every connected session."""
    shared_app = adventure_factory()  # instance, not factory
    server = mount(shared_app, mode=ServingMode.STEP, name="shared-no-reset")
    async with Client(server) as client:
        r = await client.call_tool("reset_session", {})
        out = r.structured_content
        assert out["error"] == "reset_not_supported"
        assert "shared-app mode" in out["reason"]


@pytest.mark.asyncio
async def test_reset_session_mid_workflow_works():
    """Reset doesn't only work from terminal nodes."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="mid-reset")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("step", {"action": "go_to_library", "inputs": {}})
        await client.call_tool("step", {"action": "take_key", "inputs": {}})
        # In the library with key, mid-workflow.
        state_before = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state_before["has_key"] is True

        r = await client.call_tool("reset_session", {})
        out = r.structured_content
        assert out["state"]["has_key"] is False
        assert out["valid_next_actions"] == ["enter_foyer"]


@pytest.mark.asyncio
async def test_graph_resource_advertises_meta_tools():
    """A client reading burr://graph at cold start should learn about
    reset_session as a meta tool, not just the FSM actions."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="meta-tools-test")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("burr://graph"))[0].text)
        assert "meta_tools" in graph
        meta_names = [m["name"] for m in graph["meta_tools"]]
        assert "reset_session" in meta_names


@pytest.mark.asyncio
async def test_two_sessions_reset_independently():
    """One session resetting doesn't affect another session's state."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="indep-reset")

    async with Client(server) as client_a:
        await client_a.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client_a.call_tool("step", {"action": "go_to_library", "inputs": {}})
        await client_a.call_tool("step", {"action": "take_key", "inputs": {}})

        async with Client(server) as client_b:
            await client_b.call_tool("step", {"action": "enter_foyer", "inputs": {}})

            # B resets its own session.
            await client_b.call_tool("reset_session", {})
            state_b = json.loads((await client_b.read_resource("burr://state"))[0].text)
            assert state_b.get("room") is None  # back to initial

        # A is untouched.
        state_a = json.loads((await client_a.read_resource("burr://state"))[0].text)
        assert state_a["room"] == "library"
        assert state_a["has_key"] is True


@pytest.mark.asyncio
async def test_simple_hand_built_factory_works():
    """Sanity: reset works against a minimal hand-built factory too,
    not just example factories."""

    @action(reads=[], writes=["counter"])
    def increment(state: State) -> State:
        return state.update(counter=state.get("counter", 0) + 1)

    def factory():
        return (
            ApplicationBuilder()
            .with_actions(increment=increment)
            .with_transitions(("increment", "increment"))
            .with_state(counter=0)
            .with_entrypoint("increment")
            .build()
        )

    server = mount(factory, mode=ServingMode.STEP, name="counter-reset")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "increment", "inputs": {}})
        await client.call_tool("step", {"action": "increment", "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["counter"] == 2

        await client.call_tool("reset_session", {})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["counter"] == 0
