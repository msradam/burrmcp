"""burr://graph: static topology description for cold-start discovery.

The graph resource lets a connecting client (typically an LLM) learn
the full FSM shape in one read, without trial-and-error or repeated
state polling. It's computed at mount time and never changes within
a session.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client

from burrmcp import ServingMode, mount


@action(reads=[], writes=["stage"])
def start(state: State, name: str, optional_note: str = "") -> State:
    """Open the workflow with the given name."""
    return state.update(stage="open", name=name)


@action(reads=["stage"], writes=["stage"])
def middle(state: State) -> State:
    """Advance to the middle stage."""
    return state.update(stage="middle")


@action(reads=["stage"], writes=["stage"])
def finish(state: State) -> State:
    """Wrap up."""
    return state.update(stage="finished")


def _branchy_app():
    return (
        ApplicationBuilder()
        .with_actions(start=start, middle=middle, finish=finish)
        .with_transitions(
            ("start", "middle"),
            ("middle", "finish", Condition.expr("stage == 'middle'")),
        )
        .with_state(stage="new")
        .with_entrypoint("start")
        .build()
    )


@pytest.mark.asyncio
async def test_graph_resource_describes_actions():
    server = mount(_branchy_app, mode=ServingMode.STEP, name="graph-test")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("burr://graph"))[0].text)

        assert graph["name"] == "graph-test"
        assert graph["entrypoint"] == "start"

        by_name = {a["name"]: a for a in graph["actions"]}
        assert set(by_name) == {"start", "middle", "finish"}

        # Start action's metadata.
        s = by_name["start"]
        assert s["description"] == "Open the workflow with the given name."
        assert s["reads"] == []
        assert s["writes"] == ["stage"]
        assert s["required_inputs"] == ["name"]
        assert s["optional_inputs"] == ["optional_note"]


@pytest.mark.asyncio
async def test_graph_resource_describes_transitions_with_conditions():
    server = mount(_branchy_app, mode=ServingMode.STEP, name="t-test")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("burr://graph"))[0].text)

        transitions = graph["transitions"]
        unconditional = [t for t in transitions if t["condition"] is None]
        conditional = [t for t in transitions if t["condition"] is not None]

        # start -> middle has no explicit condition.
        assert {"from": "start", "to": "middle", "condition": None} in unconditional

        # middle -> finish is gated on stage == 'middle'.
        assert len(conditional) == 1
        assert conditional[0]["from"] == "middle"
        assert conditional[0]["to"] == "finish"
        assert "stage == 'middle'" in conditional[0]["condition"]


@pytest.mark.asyncio
async def test_graph_resource_is_constant_across_reads():
    """The resource is computed once at mount time; repeated reads
    return identical data even after the FSM advances."""
    server = mount(_branchy_app, mode=ServingMode.STEP, name="constant-test")
    async with Client(server) as client:
        before = (await client.read_resource("burr://graph"))[0].text
        await client.call_tool("step", {"action": "start", "inputs": {"name": "x"}})
        after = (await client.read_resource("burr://graph"))[0].text
        assert before == after


@pytest.mark.asyncio
async def test_instructions_include_discovery_hint():
    """Server instructions point at burr://graph so the model sees it
    before its first tool call."""
    server = mount(_branchy_app, mode=ServingMode.STEP, name="hint-test")
    # FastMCP exposes the server instructions via the initialize response;
    # we read them directly off the server object since the in-process
    # Client doesn't surface them through a separate API.
    assert "burr://graph" in (server.instructions or "")


@pytest.mark.asyncio
async def test_user_instructions_preserved_alongside_hint():
    """When the user passes their own instructions, the hint appends;
    the user's text isn't dropped."""
    user_text = "Read this carefully: incidents are P1/P2/P3 only."
    server = mount(
        _branchy_app,
        mode=ServingMode.STEP,
        name="combined",
        instructions=user_text,
    )
    assert user_text in (server.instructions or "")
    assert "burr://graph" in (server.instructions or "")
