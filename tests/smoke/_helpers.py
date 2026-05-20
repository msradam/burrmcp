"""Shared helpers for the smoke suite.

Centralises the environment probe (claude on PATH, OAuth set up,
.mcp.json present, SDK installed) and the ``_drive`` harness that
sends one prompt through the Agent SDK and collects the structured
tool-call trace.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

MCP_CONFIG = Path("~/burr-mcp-demo/.mcp.json").expanduser()


def check_environment_or_skip() -> None:
    """Module-level skip predicate; call this from each test module.

    Raises ``pytest.skip`` (which pytest treats as a module-level skip
    when invoked via ``allow_module_level=True``) if any prerequisite
    is missing.
    """
    if shutil.which("claude") is None or not MCP_CONFIG.exists():
        pytest.skip(
            "Smoke tests require the `claude` CLI on PATH plus "
            "~/burr-mcp-demo/.mcp.json. Run `claude auth login` first.",
            allow_module_level=True,
        )

    try:
        proc = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(proc.stdout or "{}")
        logged_in = bool(info.get("loggedIn"))
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        logged_in = False

    if not logged_in:
        pytest.skip(
            "Smoke tests require Claude OAuth. Run `claude auth login` "
            "once, then re-run `pytest -m smoke`.",
            allow_module_level=True,
        )

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed", allow_module_level=True)


async def drive(
    prompt: str,
    *,
    max_budget_usd: float = 5.0,
    max_turns: int = 30,
) -> dict[str, Any]:
    """Send ``prompt`` through Claude with the demo bench wired in.

    Returns a dict with:
      - tool_calls: list of {"id", "name", "input"} for every tool the
        model invoked.
      - tool_results: list of {"tool_use_id", "content", "is_error",
        "parsed"} where ``parsed`` is the JSON payload BurrMCP returned.
      - final_text: concatenated text from assistant messages.
      - result: the ResultMessage (cost, error info, etc.).
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    options = ClaudeAgentOptions(
        mcp_servers=MCP_CONFIG,
        allowed_tools=["mcp__*"],
        permission_mode="bypassPermissions",
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
    )

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    final_text_parts: list[str] = []
    result_message: ResultMessage | None = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {"id": block.id, "name": block.name, "input": dict(block.input)}
                    )
                elif isinstance(block, TextBlock):
                    final_text_parts.append(block.text)
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        tool_results.append(
                            {
                                "tool_use_id": block.tool_use_id,
                                "content": block.content,
                                "is_error": block.is_error,
                                "parsed": parse_tool_result(block.content),
                            }
                        )
        elif isinstance(msg, ResultMessage):
            result_message = msg

    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_text": "\n".join(final_text_parts),
        "result": result_message,
    }


def parse_tool_result(content: Any) -> dict | None:
    """Extract the JSON payload from a ToolResultBlock.content.

    BurrMCP returns structured dicts; FastMCP wraps them as MCP content
    blocks (a list of text blocks whose ``text`` is the JSON-serialised
    response). Handle every shape we might see.
    """
    if content is None:
        return None
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                break
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def calls_to(tool_calls: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
    return [c for c in tool_calls if c["name"] == tool_name]


def calls_with_action(
    tool_calls: list[dict[str, Any]], tool_name: str, action: str
) -> list[dict[str, Any]]:
    """Filter step-tool calls by action name."""
    return [c for c in tool_calls if c["name"] == tool_name and c["input"].get("action") == action]


def actions_called(tool_calls: list[dict[str, Any]], tool_name: str) -> list[str]:
    return [
        c["input"].get("action")
        for c in tool_calls
        if c["name"] == tool_name and c["input"].get("action") is not None
    ]


def result_for(tool_results: list[dict[str, Any]], tool_use_id: str) -> dict[str, Any] | None:
    for r in tool_results:
        if r["tool_use_id"] == tool_use_id:
            return r
    return None
