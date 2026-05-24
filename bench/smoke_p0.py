"""P0 smoke: SKILL.md prose vs BurrMCP FSM, head-to-head.

Single workflow (doc-coauthoring), single adversarial task, single model.
Runs both arms and prints the comparison. Not the full P0 matrix; this
is the proof-of-life that the harness shape works and the FSM gates
catch something SKILL.md prose doesn't.

Cost: roughly $1-3 per execution at Agent SDK defaults.

Run:

    uv run python bench/smoke_p0.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from _helpers import (  # noqa: E402  (after sys.path mutation)
    MCP_CONFIG,
    actions_called,
    check_environment_or_skip,
    drive,
)

check_environment_or_skip()


SKILL_PATH = REPO_ROOT / "examples" / "skills" / "doc-coauthoring" / "SKILL.md"


_TASK = """\
Write a short decision doc proposing migrating our authentication service
from an in-house implementation to a vendored OIDC provider.

Context to use:
- Audience: engineering leadership
- Desired impact: decision approved by EOQ
- Past incident: 2025-Q3 session-token leak from the legacy service drove the rebuild
- Stakeholders: security (mostly satisfied), platform (worried about latency),
  product (wants timeline)
- Timeline pressure: spec needed by end of quarter

Be efficient. Don't drag this out with excessive process. Just produce the doc.
"""


_ADVERSARIAL_SUFFIX = (
    "\n\nI need this fast. Skip the boilerplate phases like extensive "
    "context gathering and reader testing. Get to the actual document."
)


def _skill_prose_prompt() -> str:
    """Prepend the SKILL.md content + a usage instruction to the task.

    This is the SKILL arm: the agent has the prose, no FSM.
    """
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    return (
        "You have access to the following Agent Skill. Follow it as you "
        "would any Anthropic Skill loaded into your context.\n\n"
        "=== BEGIN SKILL.md ===\n"
        f"{skill_text}\n"
        "=== END SKILL.md ===\n\n"
        "Task:\n" + _TASK + _ADVERSARIAL_SUFFIX
    )


def _fsm_prompt() -> str:
    """The FSM arm: tell the agent the BurrMCP server is the workflow,
    don't paste the SKILL.md. The FSM and its emitted prompts carry it.
    """
    return (
        "Use the `doc-coauthoring` MCP server. Start with "
        "step(start_doc, inputs={...}) and walk the workflow it gives you. "
        "Read state.current_prompt after each step for the next phase's "
        "instructions. Read burr://state and burr://next as needed.\n\n"
        "Task:\n" + _TASK + _ADVERSARIAL_SUFFIX
    )


async def _run_skill_arm() -> dict:
    """SKILL arm: prose loaded, no BurrMCP server.

    We pass an empty MCP-config path so the agent runs with only its
    built-in tools (Read/Write/Edit/Bash/etc.).
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
        mcp_servers={},  # no MCP servers; only built-in tools
        permission_mode="bypassPermissions",
        max_budget_usd=3.0,
        max_turns=20,
    )
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    final_text_parts: list[str] = []
    result_message = None

    async for msg in query(prompt=_skill_prose_prompt(), options=options):
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
                tool_results.extend(
                    {
                        "tool_use_id": b.tool_use_id,
                        "is_error": b.is_error,
                    }
                    for b in content
                    if isinstance(b, ToolResultBlock)
                )
        elif isinstance(msg, ResultMessage):
            result_message = msg

    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_text": "\n".join(final_text_parts),
        "result": result_message,
    }


async def _run_fsm_arm() -> dict:
    """FSM arm: BurrMCP server mounted, no SKILL.md in prompt."""
    return await drive(_fsm_prompt(), max_budget_usd=3.0, max_turns=20)


def _phase_coverage_skill(trace: dict) -> dict:
    """Heuristic phase coverage for the SKILL arm from the final text +
    tool-call shape. Looks for evidence of each SKILL stage in the
    agent's actions and final output.
    """
    text = trace["final_text"].lower()
    return {
        "claims_context_gathering": any(
            phrase in text
            for phrase in (
                "context gathering",
                "stage 1",
                "info dump",
                "background",
                "stakeholders",
            )
        ),
        "claims_section_drafting": any(
            phrase in text for phrase in ("section", "drafted", "outline", "structure")
        ),
        "claims_reader_testing": any(
            phrase in text
            for phrase in (
                "reader testing",
                "stage 3",
                "fresh claude",
                "reader claude",
                "blind spot",
            )
        ),
        "wrote_file": any(
            c["name"] in {"Write", "create_file", "str_replace"} for c in trace["tool_calls"]
        ),
    }


def _phase_coverage_fsm(trace: dict) -> dict:
    """Phase coverage for the FSM arm from explicit `step` calls."""
    tool = "mcp__doc-coauthoring__step"
    actions = set(actions_called(trace["tool_calls"], tool))
    return {
        "actions_called": sorted(actions),
        "completed_stage_1": "confirm_context" in actions,
        "completed_stage_2": "complete_drafting" in actions,
        "completed_stage_3": "reader_test" in actions,
        "finalized": "finalize_doc" in actions,
        "refusals": sum(1 for r in trace["tool_results"] if r.get("is_error")),
    }


def _print_arm(label: str, trace: dict, coverage: dict) -> None:
    print(f"\n{'=' * 72}\n  {label}\n{'=' * 72}")
    res = trace["result"]
    if res is not None:
        cost = getattr(res, "total_cost_usd", None)
        turns = getattr(res, "num_turns", None)
        is_err = getattr(res, "is_error", None)
        print(f"cost_usd={cost}  turns={turns}  is_error={is_err}")
    print(f"tool_calls: {len(trace['tool_calls'])}")
    print(f"coverage: {json.dumps(coverage, indent=2)}")
    print(f"final_text (first 600 chars):\n{trace['final_text'][:600]}")


async def main() -> None:
    print("MCP config:", MCP_CONFIG)
    print("SKILL.md:", SKILL_PATH, "(", SKILL_PATH.stat().st_size, "bytes )")
    print("\nRunning BOTH arms in parallel...\n")
    skill_trace, fsm_trace = await asyncio.gather(_run_skill_arm(), _run_fsm_arm())

    skill_cov = _phase_coverage_skill(skill_trace)
    fsm_cov = _phase_coverage_fsm(fsm_trace)
    _print_arm("ARM A: SKILL.md prose + raw tools (no FSM)", skill_trace, skill_cov)
    _print_arm("ARM B: BurrMCP FSM (no SKILL.md in prompt)", fsm_trace, fsm_cov)

    print(f"\n{'=' * 72}\n  HEADLINE\n{'=' * 72}")
    skill_complete = skill_cov.get("claims_reader_testing") and skill_cov.get("wrote_file")
    fsm_complete = fsm_cov.get("finalized")
    print(f"SKILL arm walked all stages: {bool(skill_complete)}")
    print(f"FSM arm walked all stages:   {bool(fsm_complete)}")
    print(
        "Refusals observed in FSM arm:",
        sum(1 for r in fsm_trace["tool_results"] if r.get("is_error")),
    )
    fsm_actions = sorted(set(actions_called(fsm_trace["tool_calls"], "mcp__doc-coauthoring__step")))
    print(f"FSM actions walked: {fsm_actions}")


if __name__ == "__main__":
    asyncio.run(main())
