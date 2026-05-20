"""fork_at: rewind to a specific history entry and continue from there.

Lets an agent explore alternate paths from any checkpoint without
disconnecting. Implemented via the in-memory burr://history so it
works without users wiring up a Burr LocalTrackingClient.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from adventure import build_application as adventure_factory


@pytest.mark.asyncio
async def test_fork_at_appears_as_tool_and_in_meta_tools():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-tool")
    async with Client(server) as client:
        tools = {t.name for t in await client.list_tools()}
        assert "fork_at" in tools

        graph = json.loads((await client.read_resource("burr://graph"))[0].text)
        meta_names = [m["name"] for m in graph["meta_tools"]]
        assert "fork_at" in meta_names


@pytest.mark.asyncio
async def test_fork_at_rewinds_to_an_earlier_state():
    """Walk to terminal, then fork back to a mid-FSM checkpoint and
    pick a different branch from there."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-rewind")
    async with Client(server) as client:
        # Walk the win path.
        for a in [
            "enter_foyer",  # seq 0
            "go_to_library",  # seq 1
            "take_key",  # seq 2
            "go_to_hallway",  # seq 3
            "unlock_door",  # seq 4
            "go_to_garden_via_hallway",  # seq 5
        ]:
            await client.call_tool("step", {"action": a, "inputs": {}})
        # Terminal: valid is [].
        valid = json.loads((await client.read_resource("burr://next"))[0].text)
        assert valid == []

        # Fork back to after enter_foyer (seq 0).
        r = await client.call_tool("fork_at", {"sequence_id": 0})
        out = json.loads(r.content[0].text)
        assert out["action"] == "fork_at"
        assert out["result"]["from_action"] == "enter_foyer"
        assert out["state"]["room"] == "foyer"
        assert out["state"]["has_key"] is False
        # Both foyer outgoing edges should now be valid again.
        assert set(out["valid_next_actions"]) == {"go_to_library", "go_to_garden_direct"}


@pytest.mark.asyncio
async def test_fork_at_records_marker_in_history():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-history")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("step", {"action": "go_to_library", "inputs": {}})
        await client.call_tool("fork_at", {"sequence_id": 0})

        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == ["enter_foyer", "go_to_library", "fork_at"]
        fork_entry = history[2]
        assert fork_entry["refused"] is False
        assert fork_entry["inputs"]["sequence_id"] == 0


@pytest.mark.asyncio
async def test_fork_at_refuses_to_a_refusal_entry():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-refuse-target")
    async with Client(server) as client:
        # Generate a refusal: unlock_door from initial state.
        await client.call_tool("step", {"action": "unlock_door", "inputs": {}})
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        assert history[0]["refused"] is True

        r = await client.call_tool("fork_at", {"sequence_id": 0})
        out = json.loads(r.content[0].text)
        assert out["error"] == "cannot_fork_to_refusal"


@pytest.mark.asyncio
async def test_fork_at_refuses_out_of_range():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-oor")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        r = await client.call_tool("fork_at", {"sequence_id": 99})
        out = json.loads(r.content[0].text)
        assert out["error"] == "unknown_sequence_id"
        assert out["history_length"] == 1


@pytest.mark.asyncio
async def test_fork_at_refuses_to_meta_entry():
    """Forking to a fork_at or reset_session entry would be a hall of
    mirrors; the tool refuses."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-meta")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("reset_session", {})
        # Now history is: enter_foyer (seq 0), reset_session (seq 1).
        r = await client.call_tool("fork_at", {"sequence_id": 1})
        out = json.loads(r.content[0].text)
        assert out["error"] == "cannot_fork_to_meta_entry"
        assert out["action"] == "reset_session"


@pytest.mark.asyncio
async def test_fork_at_refuses_in_shared_app_mode():
    shared_app = adventure_factory()
    server = mount(shared_app, mode=ServingMode.STEP, name="shared-no-fork")
    async with Client(server) as client:
        r = await client.call_tool("fork_at", {"sequence_id": 0})
        out = json.loads(r.content[0].text)
        assert out["error"] == "fork_not_supported"


@pytest.mark.asyncio
async def test_fork_at_lets_alternate_path_complete():
    """Walk to terminal via one branch, fork back to the choice
    point, and complete via the other branch."""
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-altpath")
    async with Client(server) as client:
        # First attempt: dead-end via go_to_garden_direct.
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("step", {"action": "go_to_garden_direct", "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["room"] == "garden"
        assert state["won"] is False  # the dead-end branch

        # Fork back to seq 0 (after enter_foyer).
        await client.call_tool("fork_at", {"sequence_id": 0})
        # Try the winning path this time.
        for a in [
            "go_to_library",
            "take_key",
            "go_to_hallway",
            "unlock_door",
            "go_to_garden_via_hallway",
        ]:
            await client.call_tool("step", {"action": a, "inputs": {}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["won"] is True
