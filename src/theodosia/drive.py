"""``theodosia.drive_claude``: plug a Claude model into a mounted server and let it run.

Theodosia mounts a Burr FSM as a FastMCP server. This module is the missing
ergonomic glue between that server and a model client: connect Anthropic's
SDK, expose the four-tool surface plus the FSM's resources, and let Claude
drive until the FSM reaches a terminal action or the turn cap is hit.

What it isn't: an agent framework. The driver does not pick actions for
you; the model does. The driver does not retry, replan, or correct; the
graph's structural refusals do. The driver does the boring work: listing
tools, translating MCP tool calls into Anthropic tool-use blocks, feeding
tool_result back, and stopping at the right time.

Usage::

    from theodosia import mount, drive_claude
    from anthropic import AsyncAnthropic

    server = mount(build_application, name="coffee")
    transcript = await drive_claude(
        server,
        AsyncAnthropic(),
        prompt="Order a latte with oat milk.",
        model="claude-opus-4-7",
    )

``anthropic`` is an optional dependency (``pip install 'theodosia[claude]'``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from fastmcp import FastMCP


def _mcp_tools_to_anthropic(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """Map MCP tool definitions to Anthropic's ``tools=`` argument shape."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


async def _format_resource(client: Any, uri: str) -> str:
    """Read a Theodosia resource and return its text payload, or ``''``."""
    try:
        result = await client.read_resource(uri)
    except Exception:
        return ""
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


async def drive_claude(
    server: FastMCP,
    anthropic: AsyncAnthropic,
    *,
    prompt: str,
    model: str = "claude-opus-4-7",
    system: str | None = None,
    max_turns: int = 50,
    max_output_tokens: int = 4096,
    stop_on_terminal: bool = True,
    on_step: Any = None,
) -> dict[str, Any]:
    """Run Claude against a mounted Theodosia server until terminal or cap.

    Returns a transcript dict with ``turns`` (one entry per assistant turn),
    ``final_state`` (the Burr state at the last reachable point), and
    ``stopped_on`` (``"terminal"`` / ``"cap"`` / ``"text_only"``). The
    ``on_step`` callback, if given, is awaited with each ``(action, result)``
    so callers can stream progress.

    The model sees the FSM as four MCP tools plus the ``theodosia://`` graph,
    state, and next resources injected into its system prompt. Refusals come
    back as tool_result content with ``valid_next_actions`` populated, so the
    model can self-correct without retry prompting.
    """
    from fastmcp import Client

    transcript: dict[str, Any] = {"turns": [], "final_state": None, "stopped_on": "cap"}

    async with Client(server) as client:
        mcp_tools = await client.list_tools()
        tools = _mcp_tools_to_anthropic(mcp_tools)

        # Cold-start context: graph topology + initial state + reachable
        # actions. This is the equivalent of an LLM agent reading
        # theodosia://graph at session start.
        graph_text = await _format_resource(client, "theodosia://graph")
        state_text = await _format_resource(client, "theodosia://state")
        next_text = await _format_resource(client, "theodosia://next")

        system_lines: list[str] = []
        if system:
            system_lines.append(system)
        system_lines.append(
            "You are driving a state machine served over MCP. Use the four "
            "tools to take exactly one action at a time. Every refusal "
            "carries `valid_next_actions` you can recover from."
        )
        system_lines.extend(
            (
                "",
                "## FSM graph",
                graph_text,
                "",
                "## Current state",
                state_text,
                "",
                "## Reachable actions",
                next_text,
            )
        )
        system_prompt = "\n".join(system_lines)

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        for _ in range(max_turns):
            response = await anthropic.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                transcript["stopped_on"] = "text_only"
                break

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                args = block.input or {}
                try:
                    r = await client.call_tool(block.name, args)
                    payload = r.structured_content or {"content": str(r.content)}
                except Exception as exc:
                    payload = {"error": "tool_invocation_failed", "detail": str(exc)}
                turn = {"action": args.get("action") or block.name, "result": payload}
                transcript["turns"].append(turn)
                if on_step is not None:
                    await on_step(turn["action"], payload)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

            if stop_on_terminal:
                next_text = await _format_resource(client, "theodosia://next")
                try:
                    reachable = json.loads(next_text)
                except json.JSONDecodeError:
                    reachable = []
                if isinstance(reachable, list) and not reachable:
                    transcript["stopped_on"] = "terminal"
                    break

        state_text = await _format_resource(client, "theodosia://state")
        try:
            transcript["final_state"] = json.loads(state_text)
        except json.JSONDecodeError:
            transcript["final_state"] = state_text

    return transcript
