"""Smoke: external_tools federation, second domain (filesystem), real agent.

Wires a real Claude to BOTH:
  - the code-audit FSM (burrmcp, external_tools_filesystem demo)
  - the OFFICIAL @modelcontextprotocol/server-filesystem MCP server,
    allowed to read the shipped vuln_demo codebase.

The FSM holds no file tools; it declares per-action external_tools. The
agent reads next_external_tools and calls the filesystem server's tools
(directory_tree, read_file, search_files, ...) to audit the code, then
step()s the FSM to record findings.

Proves the federation pattern generalizes across domains (files, not k8s)
and works against a real third-party MCP server, not just a hand-rolled one.

Deselected by default (marker: smoke). Needs `claude` CLI + OAuth + npx.
Run: uv run pytest -m smoke tests/smoke/test_external_tools_filesystem.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from _helpers import check_environment_or_skip

check_environment_or_skip()

if shutil.which("npx") is None:
    pytest.skip("filesystem federation smoke needs npx on PATH", allow_module_level=True)

_AUDIT_TARGET = REPO_ROOT / "examples" / "data" / "codebase_security" / "vuln_demo"


def _mcp_servers() -> dict:
    return {
        "code-audit": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "--directory",
                str(REPO_ROOT),
                "run",
                "burrmcp",
                "serve",
                "external_tools_filesystem:build_application",
                "--app-dir",
                "examples",
                "--name",
                "code-audit",
            ],
        },
        "filesystem": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", str(_AUDIT_TARGET)],
        },
    }


async def _drive(prompt: str, *, max_turns: int = 40, max_budget_usd: float = 3.0):
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    options = ClaudeAgentOptions(
        mcp_servers=_mcp_servers(),
        allowed_tools=["mcp__*"],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )
    tool_calls: list[dict] = []
    text_parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append({"name": b.name, "input": dict(b.input)})
                elif isinstance(b, TextBlock):
                    text_parts.append(b.text)
        elif isinstance(msg, ResultMessage):
            pass
    return {"tool_calls": tool_calls, "final_text": "\n".join(text_parts)}


@pytest.mark.asyncio
async def test_fsm_orchestrates_filesystem_tools():
    out = await _drive(
        "Use the `code-audit` MCP server to run a source-code security audit "
        f"of the directory {_AUDIT_TARGET}. That FSM server has no file tools; "
        "after each step it returns next_external_tools naming which tools on "
        "the `filesystem` MCP server to call. Walk it: start_audit, survey_tree "
        "(after calling directory_tree/list_directory on the filesystem server), "
        "inspect at least two files (call read_file/search_files, then "
        "inspect_file to record each finding), then write_advisory. Report the "
        "most serious issue you found.",
        max_turns=45,
        max_budget_usd=3.0,
    )

    names = [c["name"] for c in out["tool_calls"]]
    fsm_calls = [n for n in names if n.endswith("__step")]
    fs_calls = [n for n in names if n.startswith("mcp__filesystem__")]

    assert fsm_calls, f"agent never called the FSM step tool. Tools: {sorted(set(names))}"
    assert fs_calls, (
        f"agent never called the external filesystem tools the FSM declared. "
        f"Tools: {sorted(set(names))}"
    )
    called_fs_short = {n.split("__")[-1] for n in fs_calls}
    expected = {
        "list_allowed_directories",
        "directory_tree",
        "list_directory",
        "search_files",
        "read_file",
        "read_multiple_files",
        "get_file_info",
    }
    assert called_fs_short & expected, (
        f"filesystem tools called {called_fs_short} don't match the FSM's "
        f"declared external_tools {expected}"
    )
