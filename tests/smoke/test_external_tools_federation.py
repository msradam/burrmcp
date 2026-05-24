"""Smoke: external_tools federation across two MCP servers, real agent.

Wires a real Claude (via the Agent SDK) to BOTH:
  - the k8s-incident FSM (burrmcp, external_tools_k8s demo)
  - a fake kubernetes MCP server (_fake_k8s_server.py)

The FSM holds no k8s tools; it declares per-action external_tools. The
agent should read next_external_tools off the FSM step responses and call
the corresponding tools on the kubernetes server, walking the incident to
confirm_resolution.

Asserts the federation actually happened: the agent called BOTH the FSM
step tool AND the external k8s tools the FSM pointed it at.

Deselected by default (marker: smoke). Needs `claude` CLI + OAuth.
Run: uv run pytest -m smoke tests/smoke/test_external_tools_federation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from _helpers import check_environment_or_skip

check_environment_or_skip()


_FAKE_K8S = REPO_ROOT / "tests" / "smoke" / "_fake_k8s_server.py"


def _mcp_servers() -> dict:
    """Two stdio MCP servers: the FSM playbook + the fake k8s cluster."""
    return {
        "k8s-incident": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "--directory",
                str(REPO_ROOT),
                "run",
                "burrmcp",
                "serve",
                "external_tools_k8s:build_application",
                "--app-dir",
                "examples",
                "--name",
                "k8s-incident",
            ],
        },
        "kubernetes": {
            "type": "stdio",
            "command": "uv",
            "args": ["--directory", str(REPO_ROOT), "run", "python", str(_FAKE_K8S)],
        },
    }


async def _drive_two_servers(prompt: str, *, max_turns: int = 30, max_budget_usd: float = 3.0):
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
    result = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append({"name": b.name, "input": dict(b.input)})
                elif isinstance(b, TextBlock):
                    text_parts.append(b.text)
        elif isinstance(msg, ResultMessage):
            result = msg
    return {"tool_calls": tool_calls, "final_text": "\n".join(text_parts), "result": result}


@pytest.mark.asyncio
async def test_fsm_orchestrates_external_k8s_tools():
    out = await _drive_two_servers(
        "Use the `k8s-incident` MCP server to investigate this incident: "
        "'checkout-api pods are CrashLooping'. That FSM server holds no "
        "Kubernetes tools itself; after each step it returns "
        "next_external_tools telling you which tools on the `kubernetes` MCP "
        "server to call. Walk the FSM end to end: start_incident, gather at "
        "least two observations (calling the named kubernetes tools to get "
        "real data, then record_observation), decide_remediation, and "
        "confirm_resolution. Report the root cause.",
        max_turns=40,
        max_budget_usd=3.0,
    )

    names = [c["name"] for c in out["tool_calls"]]
    fsm_calls = [n for n in names if n.endswith("__step")]
    k8s_calls = [n for n in names if n.startswith("mcp__kubernetes__")]

    # The FSM was driven.
    assert fsm_calls, f"agent never called the FSM step tool. Tools: {sorted(set(names))}"
    # The federation happened: the agent called the EXTERNAL k8s tools the
    # FSM pointed it at, not just the FSM.
    assert k8s_calls, (
        f"agent never called the external kubernetes tools the FSM declared. "
        f"Tools called: {sorted(set(names))}"
    )
    # At least one of the specific tools the FSM names in external_tools.
    called_k8s_short = {n.split("__")[-1] for n in k8s_calls}
    expected = {
        "list_pods",
        "list_events",
        "get_pod_logs",
        "describe_pod",
        "rollout_restart",
        "get_deployment",
        "top_pods",
        "scale_deployment",
    }
    assert called_k8s_short & expected, (
        f"agent called kubernetes tools {called_k8s_short} but none match the "
        f"FSM's declared external_tools {expected}"
    )
