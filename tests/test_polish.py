"""Polish surfaces added in 1.12.x: ResourcesAsTools for tools-only clients,
ctx.info per step for clients that render log notifications inline, and
Visibility-driven hiding of ``fork_from_past`` when no resume mechanism is
wired.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from coffee_order import build_application

# ── ResourcesAsTools ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resources_tool_lists_native_resources():
    """``list_resources`` is exposed as a tool and returns the same set
    of Burr resources that native ``resources/list`` would return."""
    from theodosia import ServingMode, mount

    server = mount(build_application, mode=ServingMode.STEP, name="rasr")
    async with Client(server) as client:
        tool_names = {t.name for t in await client.list_tools()}
        assert {"list_resources", "read_resource"}.issubset(tool_names)

        r = await client.call_tool("list_resources", {})
        # list_resources is from FastMCP's ResourcesAsTools transform; the
        # function returns a JSON string, so the wire form is plain text
        # content (no structured_content). Parse from content[0].
        items = json.loads(r.content[0].text)
        uris_or_templates = {i.get("uri") or i.get("uri_template") for i in items}
        # Every server registers these eight; subruns/{id} is a template.
        assert "theodosia://graph" in uris_or_templates
        assert "theodosia://state" in uris_or_templates
        assert "theodosia://next" in uris_or_templates
        assert "theodosia://history" in uris_or_templates
        assert "theodosia://trace" in uris_or_templates
        assert "theodosia://session" in uris_or_templates
        assert "theodosia://subruns" in uris_or_templates
        assert "theodosia://subruns/{subrun_id}" in uris_or_templates


@pytest.mark.asyncio
async def test_read_resource_tool_matches_native_read():
    """``read_resource`` returns the same payload a native resource read
    would, for the same URI. Lets a tools-only client (e.g. IBM Bob
    Shell) reach theodosia:// resources through the tool surface."""
    from theodosia import ServingMode, mount

    server = mount(build_application, mode=ServingMode.STEP, name="rasr2")
    async with Client(server) as client:
        native = (await client.read_resource("theodosia://graph"))[0].text
        via_tool = (
            (await client.call_tool("read_resource", {"uri": "theodosia://graph"})).content[0].text
        )
        assert via_tool == native


# ── ctx.info per step ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_emits_log_notification_per_call():
    """Each step call emits one ``notifications/message`` line that names
    the seq, the action, and the outcome. Clients that render log
    notifications inline (Bob, Claude Code streaming) get a visible
    step-by-step trail without the user expanding tool calls."""
    from theodosia import ServingMode, mount

    captured: list[str] = []

    async def on_log(message) -> None:  # MCP LogMessage params
        data = message.data
        text = data if isinstance(data, str) else (data or {}).get("msg") or str(data)
        captured.append(text)

    server = mount(build_application, mode=ServingMode.STEP, name="logs")
    async with Client(server, log_handler=on_log) as client:
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        # Refusal: not reachable, should still log a line.
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "espresso"}})

    # Per-step notifications carry the brand glyphs: ⊢ for allowed
    # transitions, × for refused ones. See src/theodosia/_responses.py.
    assert any("Step 0:" in m and "take_order" in m and "⊢" in m for m in captured)
    assert any("Step 1:" in m and "pay" in m and "⊢" in m for m in captured)
    assert any("Step 2:" in m and "take_order" in m and "×" in m for m in captured)


@pytest.mark.asyncio
async def test_reset_session_emits_log():
    from theodosia import ServingMode, mount

    captured: list[str] = []

    async def on_log(message) -> None:
        data = message.data
        captured.append(data if isinstance(data, str) else (data or {}).get("msg") or str(data))

    server = mount(build_application, mode=ServingMode.STEP, name="logs-reset")
    async with Client(server, log_handler=on_log) as client:
        await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
        await client.call_tool("reset_session", {})

    assert any("Session reset" in m for m in captured)


# ── Visibility for fork_from_past ────────────────────────────────────
# (the no-tracker hide case is asserted in test_fork_from_past.py;
# here we assert the inverse: with a tracker, it stays visible.)


@pytest.mark.asyncio
async def test_fork_from_past_visible_with_tracker(tmp_path, monkeypatch):
    """A factory that wires a LocalTrackingClient keeps fork_from_past
    visible in the tool listing."""
    from burr.core import ApplicationBuilder, State, action
    from burr.tracking.client import LocalTrackingClient

    from theodosia import ServingMode, mount

    @action(reads=[], writes=["n"])
    async def bump(state: State) -> State:
        return state.update(n=state.get("n", 0) + 1)

    monkeypatch.setenv("HOME", str(tmp_path))

    def factory():
        return (
            ApplicationBuilder()
            .with_actions(bump=bump)
            .with_transitions(("bump", "bump"))
            .with_tracker(LocalTrackingClient(project=f"tracked-{tmp_path.name}"))
            .with_state(n=0)
            .with_entrypoint("bump")
            .build()
        )

    server = mount(factory, mode=ServingMode.STEP, name="ffp-visible")
    async with Client(server) as client:
        tool_names = {t.name for t in await client.list_tools()}
        assert "fork_from_past" in tool_names
