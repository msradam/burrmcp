"""End-to-end tests for persona MCP prompts (regression: ctx-arg bug)."""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from theodosia import mount


@action(reads=[], writes=["x"])
def _go(state: State) -> State:
    return state.update(x=1)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(go=_go)
        .with_state(x=0)
        .with_entrypoint("go")
        .build()
    )


_PERSONA = "---\nname: careful\ndescription: A careful operator.\n---\nrole: {state.x}"


@pytest.mark.asyncio
async def test_persona_listed_with_no_required_arguments():
    server = mount(_factory, name="p", personas={"careful": _PERSONA})
    async with Client(server) as c:
        prompts = await c.list_prompts()
        names = [p.name for p in prompts]
        assert "theodosia/persona/careful" in names
        careful = next(p for p in prompts if p.name == "theodosia/persona/careful")
        # ctx is server-injected; it must not appear as a required client arg.
        client_args = [a.name for a in (careful.arguments or []) if a.required]
        assert client_args == [], (
            f"persona prompt should have no required client args; got {client_args}"
        )


@pytest.mark.asyncio
async def test_persona_get_prompt_returns_body_with_frame_interpolation():
    server = mount(_factory, name="p", personas={"careful": _PERSONA})
    async with Client(server) as c:
        before = await c.get_prompt("theodosia/persona/careful")
        text_before = before.messages[0].content.text
        assert "role: 0" in text_before

        await c.call_tool("step", {"action": "go", "inputs": {}})

        after = await c.get_prompt("theodosia/persona/careful")
        text_after = after.messages[0].content.text
        assert "role: 1" in text_after
