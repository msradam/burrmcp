"""Adventure-game FSM: state-traversal demo.

Mirrors Burr's ``llm-adventure-game`` example pattern. Each room is
a state, each move is a transition gated on current position plus
inventory. Verifies the happy path to the win state, the gated
refusals, and that ``done_inspecting``-style conditions read state
correctly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from adventure import build_server


@pytest.mark.asyncio
async def test_happy_path_to_garden_via_hallway():
    server = build_server()
    async with Client(server) as client:
        for action in [
            "enter_foyer",
            "go_to_library",
            "take_key",
            "go_to_hallway",
            "unlock_door",
            "go_to_garden_via_hallway",
        ]:
            r = await client.call_tool("step", {"action": action, "inputs": {}})
        out = r.structured_content
        assert out["state"]["room"] == "garden"
        assert out["state"]["won"] is True


@pytest.mark.asyncio
async def test_unlock_door_without_key_is_refused():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        await client.call_tool("step", {"action": "go_to_library", "inputs": {}})
        # Walk to hallway without taking the key.
        await client.call_tool("step", {"action": "go_to_hallway", "inputs": {}})
        r = await client.call_tool("step", {"action": "unlock_door", "inputs": {}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"
        # No legal moves from hallway without the key. Stuck.
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_garden_direct_does_not_win():
    """Going directly east from the foyer reaches the garden but
    doesn't satisfy the win condition, since ``won`` only flips on the
    hallway path."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        r = await client.call_tool("step", {"action": "go_to_garden_direct", "inputs": {}})
        out = r.structured_content
        assert out["state"]["room"] == "garden"
        assert out["state"]["won"] is False
        # No outgoing transitions; you've reached a dead end.
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_log_records_every_move():
    """Every action appends a line to state['log']."""
    server = build_server()
    async with Client(server) as client:
        for action in ["enter_foyer", "go_to_library", "take_key"]:
            await client.call_tool("step", {"action": action, "inputs": {}})
        state = json.loads((await client.read_resource("theodosia://state"))[0].text)
        log = state["log"]
        assert len(log) == 3
        assert "foyer" in log[0]
        assert "library" in log[1]
        assert "key" in log[2].lower()


@pytest.mark.asyncio
async def test_graph_resource_describes_adventure_topology():
    """The static graph resource reports the eight actions and the
    conditional edges between them."""
    server = build_server()
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        action_names = {a["name"] for a in graph["actions"]}
        assert {
            "enter_foyer",
            "go_to_library",
            "go_to_garden_direct",
            "go_to_hallway",
            "take_key",
            "unlock_door",
            "go_to_garden_via_hallway",
        } <= action_names

        # Every transition in this FSM is conditioned on state.
        unconditional = [t for t in graph["transitions"] if t["condition"] is None]
        assert unconditional == []
