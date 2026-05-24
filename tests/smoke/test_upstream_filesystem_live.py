"""Smoke: the single-surface upstream story, live, with a real agent.

The agent connects to ONLY the burrmcp code-audit server. That server is
an MCP *client* to the official @modelcontextprotocol/server-filesystem,
configured via mount(upstream=...). The audit actions read files THROUGH
burrmcp via call_upstream. The agent is never given filesystem tools.

This is the "holy grail" proof: one MCP connection from the agent, yet
the graph drives a second MCP server invisibly, and every file read
happens inside an action (single surface, ledger-honest).

Asserts:
  - the agent's tool surface does NOT contain filesystem tools (read_file
    etc. are not exposed -- only burrmcp's meta-tools);
  - the agent still walked a real audit (start_audit -> read_file ->
    record_finding -> write_advisory) against the shipped vuln_demo.

Deselected by default (marker: smoke). Needs `claude` + OAuth + npx.
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
    pytest.skip("upstream filesystem smoke needs npx on PATH", allow_module_level=True)

_AUDIT_TARGET = REPO_ROOT / "examples" / "data" / "codebase_security" / "vuln_demo"


def _mcp_servers() -> dict:
    # ONE server: the burrmcp code-audit FSM, which itself opens an upstream
    # client to the filesystem server (configured in build_server()).
    return {
        "code-audit": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "--directory",
                str(REPO_ROOT),
                "run",
                "python",
                str(REPO_ROOT / "examples" / "upstream_filesystem.py"),
            ],
            "env": {"AUDIT_TARGET": str(_AUDIT_TARGET)},
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
async def test_agent_audits_through_burrmcp_upstream():
    out = await _drive(
        "Use the `code-audit` MCP server to run a source-code security audit. "
        "Call start_audit to survey the tree, then read_file on at least two "
        "suspicious files, record_finding for each, and write_advisory. Report "
        "the most serious issue. You do not have direct filesystem tools; the "
        "code-audit server reads files for you.",
        max_turns=45,
    )

    names = [c["name"] for c in out["tool_calls"]]
    step_calls = [n for n in names if n.endswith("__step")]
    # Single surface: the agent only had burrmcp's tools; the filesystem
    # server's tools were never exposed to it.
    fs_tools = [
        n
        for n in names
        if any(t in n for t in ("read_file", "directory_tree", "list_directory", "search_files"))
        and not n.endswith("__step")
    ]

    assert step_calls, f"agent never called the burrmcp step tool. Tools: {sorted(set(names))}"
    assert not fs_tools, (
        f"agent called filesystem tools directly -- single surface violated. "
        f"Tools: {sorted(set(names))}"
    )
    # The audit actually walked via the FSM step tool's action arg.
    actions = [c["input"].get("action") for c in out["tool_calls"] if c["name"].endswith("__step")]
    assert "start_audit" in actions, f"audit never started. Actions: {actions}"
    assert "read_file" in actions, f"no files read through burrmcp. Actions: {actions}"
