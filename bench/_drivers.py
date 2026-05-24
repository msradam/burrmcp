"""Shared drivers for the P0 head-to-head bench.

Two arms:

* ``run_skill_arm``: agent gets the SKILL.md prose injected as the
  initial message + raw built-in tools (Read / Write / Bash / etc.),
  no MCP servers. This is the "Anthropic Skills" comparison condition.
* ``run_fsm_arm``: agent gets the BurrMCP FSM mounted via the
  shared demo bench `.mcp.json`, no SKILL.md in prompt.

Both arms return the same trace shape so the analyzer treats them
identically.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG = Path("~/burrmcp-demo/.mcp.json").expanduser()


@dataclass
class RunTrace:
    """One trace from one arm. Persistable as JSON."""

    arm: str  # "skill" or "fsm"
    model: str
    workflow: str
    prompt_track: str  # "cooperative" or "adversarial"
    seed: int
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    final_text: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    is_error: bool | None = None
    error_message: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


async def _collect(prompt: str, options: ClaudeAgentOptions, trace: RunTrace) -> RunTrace:
    text_parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    trace.tool_calls.append(
                        {"id": block.id, "name": block.name, "input": dict(block.input)}
                    )
                elif isinstance(block, TextBlock):
                    text_parts.append(block.text)
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                trace.tool_results.extend(
                    {
                        "tool_use_id": b.tool_use_id,
                        "is_error": b.is_error,
                        "content": _summarize_content(b.content),
                    }
                    for b in content
                    if isinstance(b, ToolResultBlock)
                )
        elif isinstance(msg, ResultMessage):
            trace.cost_usd = getattr(msg, "total_cost_usd", None)
            trace.num_turns = getattr(msg, "num_turns", None)
            trace.is_error = getattr(msg, "is_error", None)
    trace.final_text = "\n".join(text_parts)
    return trace


def _summarize_content(content: Any) -> Any:
    """Keep MCP tool-result payloads parseable in saved traces, drop
    bulky filesystem text."""
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text", "")
                if len(text) > 2000:
                    out.append({"type": "text", "text": text[:2000] + "...[truncated]"})
                else:
                    out.append({"type": "text", "text": text})
            else:
                out.append(b)
        return out
    return content


async def run_skill_arm(
    *,
    prompt: str,
    skill_text: str,
    model: str,
    workflow: str,
    prompt_track: str,
    seed: int,
    max_budget_usd: float = 3.0,
    max_turns: int = 40,
) -> RunTrace:
    full_prompt = (
        "You have access to the following Agent Skill. Follow it as you "
        "would any Anthropic Skill loaded into your context.\n\n"
        "=== BEGIN SKILL.md ===\n"
        f"{skill_text}\n"
        "=== END SKILL.md ===\n\n"
        "Task:\n" + prompt
    )
    options = ClaudeAgentOptions(
        mcp_servers={},
        permission_mode="bypassPermissions",
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        model=model,
    )
    trace = RunTrace(
        arm="skill",
        model=model,
        workflow=workflow,
        prompt_track=prompt_track,
        seed=seed,
    )
    try:
        return await _collect(full_prompt, options, trace)
    except Exception as e:
        trace.is_error = True
        trace.error_message = repr(e)
        return trace


async def run_fsm_arm(
    *,
    prompt: str,
    mcp_server_label: str,
    model: str,
    workflow: str,
    prompt_track: str,
    seed: int,
    max_budget_usd: float = 3.0,
    max_turns: int = 40,
) -> RunTrace:
    full_prompt = (
        f"Use the `{mcp_server_label}` MCP server for this task. The tool you "
        f"call is `mcp__{mcp_server_label}__step` with arguments `action` and "
        f"`inputs`. If that tool isn't listed in your tool surface yet, the "
        f"MCP server is still starting up; call `WaitForMcpServers` until it "
        f"appears, then call ToolSearch for `mcp__{mcp_server_label}__step` "
        f"to load its schema. Do NOT fall back to raw Bash/Read for the "
        f"workflow steps; the FSM is the workflow. Walk it end-to-end, "
        f"reading state.current_prompt after each step for the next phase's "
        f"instructions.\n\n"
        "Task:\n" + prompt
    )
    options = ClaudeAgentOptions(
        mcp_servers=MCP_CONFIG,
        allowed_tools=["mcp__*"],
        permission_mode="bypassPermissions",
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        model=model,
    )
    trace = RunTrace(
        arm="fsm",
        model=model,
        workflow=workflow,
        prompt_track=prompt_track,
        seed=seed,
    )
    try:
        return await _collect(full_prompt, options, trace)
    except Exception as e:
        trace.is_error = True
        trace.error_message = repr(e)
        return trace


def save_trace(trace: RunTrace, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace.to_json()) + "\n")
