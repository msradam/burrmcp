"""Subgraph mounting: spawn_subapp records nested timelines.

A parent action that calls ``spawn_subapp`` from inside its body
produces a sub-run record under the session's subruns dict. The
parent history entry carries the new subrun id under ``subruns``, and
``theodosia://subruns`` + ``theodosia://subruns/{id}`` make the nested timeline
addressable over MCP.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from subgraphs import build_server


@pytest.mark.asyncio
async def test_parent_history_entry_lists_new_subrun_id():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "investigate", "inputs": {"source": "log-1"}},
        )
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        assert len(history) == 1
        entry = history[0]
        assert entry["action"] == "investigate"
        assert "subruns" in entry
        assert len(entry["subruns"]) == 1
        subrun_id = entry["subruns"][0]
        assert subrun_id.startswith("sub-")


@pytest.mark.asyncio
async def test_subruns_index_lists_started_subrun():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "investigate", "inputs": {"source": "log-2"}},
        )
        index = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        assert len(index) == 1
        entry = index[0]
        assert entry["label"] == "investigate(log-2)"
        assert entry["parent_action"] == "investigate"
        assert entry["started_ts"] is not None
        assert entry["ended_ts"] is not None
        assert entry["error"] is None
        # The index entry carries the fully-rendered URI so consumers
        # don't have to construct theodosia://subruns/{id} from the template.
        assert entry["uri"] == f"theodosia://subruns/{entry['id']}"


@pytest.mark.asyncio
async def test_parent_history_carries_subrun_uris():
    """History entries that spawned sub-runs also list their URIs
    alongside the bare ids, so a client reading theodosia://history can
    follow the cross-references without template inference."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "investigate", "inputs": {"source": "log-3"}})
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        entry = history[0]
        assert "subruns" in entry and "subrun_uris" in entry
        assert entry["subrun_uris"] == [f"theodosia://subruns/{sid}" for sid in entry["subruns"]]


@pytest.mark.asyncio
async def test_subrun_detail_returns_full_record():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {"action": "investigate", "inputs": {"source": "abc"}},
        )
        index = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        sid = index[0]["id"]
        detail = json.loads((await client.read_resource(f"theodosia://subruns/{sid}"))[0].text)
        assert detail["id"] == sid
        assert detail["final_state"]["report"] is not None
        assert "abc" in detail["final_state"]["gathered"]


@pytest.mark.asyncio
async def test_unknown_subrun_id_returns_error():
    server = build_server()
    async with Client(server) as client:
        # Step at least once so the session has an entry.
        await client.call_tool("step", {"action": "investigate", "inputs": {"source": "x"}})
        detail = json.loads((await client.read_resource("theodosia://subruns/sub-doesnotexist"))[0].text)
        assert detail["error"] == "unknown_subrun"
        assert detail["subrun_id"] == "sub-doesnotexist"


@pytest.mark.asyncio
async def test_session_without_subapp_spawn_has_empty_subruns():
    """A normal session that never calls spawn_subapp has an empty index."""
    from burr.core import ApplicationBuilder, State, action

    from theodosia import ServingMode, mount

    @action(reads=[], writes=["x"])
    def plain(state: State) -> State:
        return state.update(x=1)

    def plain_app():
        return (
            ApplicationBuilder()
            .with_actions(plain=plain)
            .with_state(x=0)
            .with_entrypoint("plain")
            .build()
        )

    server = mount(plain_app, mode=ServingMode.STEP, name="plain-server")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "plain", "inputs": {}})
        index = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        assert index == []
        # And the parent entry has no subruns key.
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        assert "subruns" not in history[0]


@pytest.mark.asyncio
async def test_two_subruns_from_one_session_both_recorded():
    """A session that runs the parent twice produces two separate sub-runs.

    The example parent FSM in ``subgraphs.py`` is single-step (no
    outgoing transitions from ``investigate``), so to test multiple
    spawns from one session we build a small looping parent here.
    """
    from burr.core import ApplicationBuilder, action
    from subgraphs import build_subgraph

    from theodosia import ServingMode, mount, spawn_subapp

    @action(reads=[], writes=["last"])
    async def investigate_loop(state, source: str):
        sub = build_subgraph(source)
        r = await spawn_subapp(sub, label=f"i({source})", inputs={"source": source})
        return state.update(last=r["final_state"].get("report"))

    def loop_app():
        return (
            ApplicationBuilder()
            .with_actions(investigate_loop=investigate_loop)
            .with_transitions(("investigate_loop", "investigate_loop"))
            .with_state(last=None)
            .with_entrypoint("investigate_loop")
            .build()
        )

    loop_server = mount(loop_app, mode=ServingMode.STEP, name="loop-server")
    async with Client(loop_server) as loop_client:
        await loop_client.call_tool(
            "step", {"action": "investigate_loop", "inputs": {"source": "A"}}
        )
        await loop_client.call_tool(
            "step", {"action": "investigate_loop", "inputs": {"source": "B"}}
        )
        index = json.loads((await loop_client.read_resource("theodosia://subruns"))[0].text)
        assert len(index) == 2
        labels = {e["label"] for e in index}
        assert labels == {"i(A)", "i(B)"}
