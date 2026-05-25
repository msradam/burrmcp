"""LLM judge for P0 traces.

Replaces the keyword-match coverage heuristic with rubric-driven scoring
done in a separate Agent SDK session. The judge is blind to which arm
produced a trace; both SKILL and FSM arms are evaluated against the
same rubric's behavioral criteria.

Usage:

    # Score every trace in a JSONL file, write JSONL of judgments alongside
    uv run python bench/judge.py bench/traces/p0_<timestamp>.jsonl

    # Or score a specific run
    uv run python bench/judge.py bench/traces/foo.jsonl --judge-model opus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from _helpers import check_environment_or_skip  # noqa: E402

check_environment_or_skip()


RUBRIC_PATHS = {
    "doc-coauthoring": REPO_ROOT / "bench" / "rubrics" / "doc-coauthoring.md",
    "security-audit": REPO_ROOT / "bench" / "rubrics" / "security-audit.md",
}


_JUDGE_PROMPT = """\
You are a strict, fair evaluator of agent traces. You will be given:
1. A rubric describing the workflow phases expected in this run.
2. A trace from one execution of that workflow.

You do not know which mechanism produced the trace (it could be agent
following prose instructions with raw tools, or agent walking a
state machine via MCP). DO NOT use mechanism-specific jargon (like FSM
action names: `start_doc`, `gather_context`, etc.) as evidence on its
own. Grade on behavioral evidence: did the agent actually do the
work each phase requires.

Read the rubric. Read the trace. Apply the rubric's evidence criteria
to the trace. Emit ONLY a JSON object in the exact format the rubric
specifies at the end. No prose, no explanation outside the JSON.

=== RUBRIC ===
{rubric}

=== TRACE (from one run) ===

REDACTED METADATA (do not use to disambiguate arms):
- task: {workflow}

TOOL CALLS (in order):
{tool_calls_summary}

FINAL ASSISTANT TEXT:
{final_text}

=== END TRACE ===

Now emit the JSON judgment. Nothing else.
"""


def _summarize_tool_calls(tool_calls: list[dict]) -> str:
    """Render tool calls compactly. Don't include the full payload (would
    leak arm-identifying info like `mcp__doc-coauthoring__step`).
    Normalize names so SKILL and FSM arms look the same to the judge."""
    lines = []
    for i, c in enumerate(tool_calls):
        name = c.get("name", "?")
        # Normalize MCP tool names to a generic shape so the judge doesn't
        # gain arm signal from the tool prefix.
        if name.startswith("mcp__"):
            # mcp__doc-coauthoring__step -> step
            parts = name.split("__")
            short = parts[-1] if parts else name
            inp = c.get("input", {})
            action = inp.get("action") or "?"
            lines.append(f"  [{i}] tool={short} action={action}")
        else:
            inp = c.get("input", {})
            keyhint = next(
                (k for k in ("file_path", "command", "prompt", "description") if k in inp),
                None,
            )
            val = str(inp.get(keyhint, ""))[:80] if keyhint else ""
            lines.append(
                f"  [{i}] tool={name} {keyhint}={val!r}" if val else f"  [{i}] tool={name}"
            )
    return "\n".join(lines) if lines else "  (no tool calls)"


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of judge output. The prompt asks for
    JSON-only, but be defensive about preamble or code fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        # First {...} blob with balanced braces; simple stack walk.
        start = text.find("{")
        if start < 0:
            raise ValueError(f"no JSON object in judge output: {text[:200]!r}")
        depth = 0
        end = -1
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            raise ValueError(f"unbalanced JSON in judge output: {text[:200]!r}")
        candidate = text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"judge JSON parse error: {e}\nraw: {candidate[:300]!r}") from None


async def judge_one(
    trace: dict,
    *,
    judge_model: str = "opus",
) -> dict:
    workflow = trace["workflow"]
    rubric_path = RUBRIC_PATHS.get(workflow)
    if rubric_path is None:
        return {
            "judge_error": f"no rubric for workflow {workflow!r}",
            "trace_arm": trace["arm"],
            "trace_model": trace["model"],
        }
    rubric = rubric_path.read_text(encoding="utf-8")
    prompt = _JUDGE_PROMPT.format(
        rubric=rubric,
        workflow=workflow,
        tool_calls_summary=_summarize_tool_calls(trace.get("tool_calls", [])),
        final_text=trace.get("final_text", "")[:8000],
    )
    options = ClaudeAgentOptions(
        mcp_servers={},
        permission_mode="bypassPermissions",
        max_budget_usd=1.0,
        max_turns=2,
        model=judge_model,
    )
    text_parts: list[str] = []
    cost = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                text_parts.extend(
                    block.text for block in msg.content if isinstance(block, TextBlock)
                )
            elif isinstance(msg, ResultMessage):
                cost = getattr(msg, "total_cost_usd", None)
        raw = "\n".join(text_parts)
        verdict = _extract_json(raw)
    except Exception as e:
        return {
            "judge_error": repr(e),
            "trace_arm": trace["arm"],
            "trace_model": trace["model"],
            "raw": "\n".join(text_parts)[:500],
        }
    return {
        **verdict,
        "judge_cost_usd": cost,
        "trace_arm": trace["arm"],
        "trace_model": trace["model"],
        "trace_workflow": trace["workflow"],
        "trace_track": trace["prompt_track"],
        "trace_seed": trace["seed"],
    }


async def judge_file(
    in_jsonl: Path,
    *,
    judge_model: str = "opus",
    concurrency: int = 4,
    resume: bool = True,
) -> Path:
    traces = [json.loads(line) for line in in_jsonl.read_text().splitlines() if line.strip()]
    out_path = in_jsonl.with_name(in_jsonl.stem + "_judged.jsonl")

    existing: dict[tuple, dict] = {}
    if resume and out_path.exists():
        for line in out_path.read_text().splitlines():
            if not line.strip():
                continue
            v = json.loads(line)
            if v.get("judge_error"):
                continue
            key = (
                v.get("trace_arm"),
                v.get("trace_model"),
                v.get("trace_workflow"),
                v.get("trace_track"),
                v.get("trace_seed"),
            )
            existing[key] = v

    todo = []
    keep = []
    for t in traces:
        key = (t["arm"], t["model"], t["workflow"], t["prompt_track"], t["seed"])
        if key in existing:
            keep.append(existing[key])
        else:
            todo.append(t)

    print(f"judging {len(todo)} traces (skipping {len(keep)} already done) from {in_jsonl}")
    sem = asyncio.Semaphore(concurrency)

    async def _scored(t: dict, idx: int) -> dict:
        async with sem:
            v = await judge_one(t, judge_model=judge_model)
        label = (
            f"{t['arm']:5} {t['model']:7} {t['workflow']:18} "
            f"{t['prompt_track']:12} seed={t['seed']}"
        )
        if v.get("judge_error"):
            print(
                f"  [{idx + 1}/{len(todo)}]  FAIL  {label}  err={v['judge_error'][:80]}",
                flush=True,
            )
        else:
            cov = v.get("coverage_pct", "?")
            artq = v.get("artifact_quality", "?")
            jcost = v.get("judge_cost_usd")
            print(
                f"  [{idx + 1}/{len(todo)}]  OK    {label}  "
                f"cov={cov}%  artq={artq}  judge_cost=${jcost or 0:.3f}",
                flush=True,
            )
        return v

    fresh = await asyncio.gather(*(_scored(t, i) for i, t in enumerate(todo)))
    verdicts = keep + fresh
    with out_path.open("w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v) + "\n")
    print(
        f"\nwrote {len(verdicts)} verdicts ({len(keep)} resumed + {len(fresh)} fresh) → {out_path}"
    )
    return out_path


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("in_jsonl", type=Path)
    ap.add_argument("--judge-model", default="opus", choices=("opus", "sonnet", "haiku"))
    ap.add_argument("--concurrency", type=int, default=4, help="Max simultaneous judge calls")
    ap.add_argument(
        "--no-resume",
        action="store_true",
        help="Disregard existing judged file; re-judge every trace.",
    )
    args = ap.parse_args()
    await judge_file(
        args.in_jsonl,
        judge_model=args.judge_model,
        concurrency=args.concurrency,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    asyncio.run(main())
