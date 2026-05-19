"""Chargen FSM: sequential-narrowing demo.

Six stages run in strict order. Tests cover the happy path through to
``finalize``, the refusal-on-skip pattern that's the whole point of
the demo, per-action input validation (point-buy total, class skill
list), and that ``burr://state`` shows the in-progress sheet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from chargen import build_server  # noqa: E402


@pytest.mark.asyncio
async def test_happy_path_through_all_six_stages():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "begin", "inputs": {"name": "Aelin"}})
        await client.call_tool("step", {"action": "choose_race", "inputs": {"race": "elf"}})
        await client.call_tool(
            "step",
            {"action": "choose_class", "inputs": {"class_name": "rogue"}},
        )
        # Point-buy 27: 15+14+13+10+10+8 -> 9+7+5+2+2+0 = 25. Try a valid combo.
        # 15+15+13+8+8+8 -> 9+9+5+0+0+0 = 23. Need 27.
        # 15+14+12+10+10+8 -> 9+7+4+2+2+0 = 24.
        # 15+14+13+12+10+8 -> 9+7+5+4+2+0 = 27. ✓
        await client.call_tool(
            "step",
            {
                "action": "assign_stats",
                "inputs": {
                    "STR": 8,
                    "DEX": 15,
                    "CON": 13,
                    "INT": 12,
                    "WIS": 10,
                    "CHA": 14,
                },
            },
        )
        await client.call_tool(
            "step",
            {
                "action": "pick_skills",
                "inputs": {"skills": ["stealth", "perception"]},
            },
        )
        await client.call_tool("step", {"action": "equip", "inputs": {}})
        r = await client.call_tool("step", {"action": "finalize", "inputs": {}})
        out = json.loads(r.content[0].text)
        sheet = out["state"]["sheet"]
        assert sheet["name"] == "Aelin"
        assert sheet["race"] == "elf"
        assert sheet["class"] == "rogue"
        assert sheet["skills"] == ["stealth", "perception"]
        assert sheet["stats"]["DEX"] == 15
        assert "shortsword" in sheet["equipment"]


@pytest.mark.asyncio
async def test_skipping_ahead_to_finalize_is_refused():
    """The whole point of the demo: agent calls ``finalize`` first and
    the FSM refuses because ``begin`` is the only valid next."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "finalize", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert "begin" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_cannot_assign_stats_before_choosing_class():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "begin", "inputs": {"name": "X"}})
        await client.call_tool("step", {"action": "choose_race", "inputs": {"race": "human"}})
        # Skip choose_class.
        r = await client.call_tool(
            "step",
            {
                "action": "assign_stats",
                "inputs": {
                    "STR": 8,
                    "DEX": 8,
                    "CON": 8,
                    "INT": 8,
                    "WIS": 8,
                    "CHA": 8,
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["choose_class"]


@pytest.mark.asyncio
async def test_point_buy_total_validated():
    """Stat assignment that doesn't spend exactly 27 points is rejected
    by the action's input validation, not the transition layer."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "begin", "inputs": {"name": "X"}})
        await client.call_tool("step", {"action": "choose_race", "inputs": {"race": "human"}})
        await client.call_tool(
            "step",
            {"action": "choose_class", "inputs": {"class_name": "fighter"}},
        )
        # All 8s spends 0 points, far below 27.
        r = await client.call_tool(
            "step",
            {
                "action": "assign_stats",
                "inputs": {
                    "STR": 8,
                    "DEX": 8,
                    "CON": 8,
                    "INT": 8,
                    "WIS": 8,
                    "CHA": 8,
                },
            },
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert "27" in out["error_message"]


@pytest.mark.asyncio
async def test_skills_must_come_from_class_list():
    """Rogues cannot pick arcana; the action rejects."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "begin", "inputs": {"name": "X"}})
        await client.call_tool("step", {"action": "choose_race", "inputs": {"race": "halfling"}})
        await client.call_tool(
            "step",
            {"action": "choose_class", "inputs": {"class_name": "rogue"}},
        )
        await client.call_tool(
            "step",
            {
                "action": "assign_stats",
                "inputs": {
                    "STR": 8,
                    "DEX": 15,
                    "CON": 13,
                    "INT": 12,
                    "WIS": 10,
                    "CHA": 14,
                },
            },
        )
        r = await client.call_tool(
            "step",
            {"action": "pick_skills", "inputs": {"skills": ["stealth", "arcana"]}},
        )
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert "rogue" in out["error_message"]


@pytest.mark.asyncio
async def test_state_shows_progress_partway_through():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "begin", "inputs": {"name": "Gwen"}})
        await client.call_tool("step", {"action": "choose_race", "inputs": {"race": "dwarf"}})
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        assert state["name"] == "Gwen"
        assert state["race"] == "dwarf"
        assert state["class_"] == ""
        assert state["stage"] == "class"
