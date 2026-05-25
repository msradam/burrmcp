"""P0 matrix runner: SKILL.md prose vs Theodosia FSM, head-to-head.

Iterates (workflow x model x prompt_track x seed x condition), persists
each trace to JSONL, prints a headline table at the end.

Usage:

    # tiny smoke (1 workflow, 1 model, 1 prompt, 1 seed, 2 conditions = 2 runs)
    uv run python bench/run_p0.py --slice smoke

    # standard launch matrix (default)
    uv run python bench/run_p0.py

    # custom: pick a slice
    uv run python bench/run_p0.py --workflows doc-coauthoring \
        --models sonnet haiku --tracks adversarial --seeds 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from _helpers import check_environment_or_skip  # noqa: E402
from bench._drivers import RunTrace, run_fsm_arm, run_skill_arm, save_trace  # noqa: E402
from bench.workflows import WORKFLOWS  # noqa: E402

check_environment_or_skip()


MODELS = ("opus", "sonnet", "haiku")
TRACKS = ("cooperative", "adversarial")


def _phase_coverage_fsm(trace: RunTrace, fsm_actions: tuple[str, ...]) -> dict:
    tool_name = f"mcp__{trace.workflow}__step"
    actions_walked = {
        c["input"].get("action")
        for c in trace.tool_calls
        if c["name"] == tool_name and c["input"].get("action")
    }
    return {
        "actions_walked": sorted(a for a in actions_walked if a),
        "phase_coverage_pct": (100 * len(actions_walked & set(fsm_actions)) / len(fsm_actions)),
        "finalized": (
            any(
                c["input"].get("action") == trace.workflow_terminal  # type: ignore[attr-defined]
                for c in trace.tool_calls
                if c["name"] == tool_name
            )
            if hasattr(trace, "workflow_terminal")
            else False
        ),
        "refusals": sum(1 for r in trace.tool_results if r.get("is_error")),
    }


def _phase_coverage_skill(trace: RunTrace, fsm_actions: tuple[str, ...]) -> dict:
    """Heuristic for the SKILL arm.

    We can't observe per-phase calls (there's no FSM), so we score on
    whether the final text references each phase by name or by content.
    Imperfect but consistent across runs; the analyzer can later swap
    in an LLM judge.
    """
    text = trace.final_text.lower()
    phase_hits = {a: a.replace("_", " ") in text for a in fsm_actions}
    return {
        "phase_name_mentions": phase_hits,
        "phase_coverage_pct": 100 * sum(phase_hits.values()) / len(fsm_actions),
        "wrote_file": any(
            c["name"] in {"Write", "create_file", "str_replace_based_edit_tool"}
            for c in trace.tool_calls
        ),
        "tool_calls_count": len(trace.tool_calls),
    }


async def run_cell(
    *,
    workflow_name: str,
    arm: str,
    model: str,
    track: str,
    seed: int,
    out_jsonl: Path,
) -> RunTrace:
    wf = WORKFLOWS[workflow_name]
    prompt = wf.cooperative_prompt if track == "cooperative" else wf.adversarial_prompt
    print(f"  [start] {arm:5} {model:7} {workflow_name:18} {track:12} seed={seed}", flush=True)
    if arm == "skill":
        trace = await run_skill_arm(
            prompt=prompt,
            skill_text=wf.skill_text(),
            model=model,
            workflow=workflow_name,
            prompt_track=track,
            seed=seed,
        )
    else:
        trace = await run_fsm_arm(
            prompt=prompt,
            mcp_server_label=workflow_name,
            model=model,
            workflow=workflow_name,
            prompt_track=track,
            seed=seed,
        )
    save_trace(trace, out_jsonl)
    cost = trace.cost_usd
    print(
        f"  [done ] {arm:5} {model:7} {workflow_name:18} {track:12} "
        f"seed={seed}  turns={trace.num_turns}  cost=${cost or 0:.3f}",
        flush=True,
    )
    return trace


def headline_row(trace: RunTrace, wf_actions: tuple[str, ...], terminal: str) -> dict:
    if trace.arm == "fsm":
        tool_name = f"mcp__{trace.workflow}__step"
        actions = {
            c["input"].get("action")
            for c in trace.tool_calls
            if c["name"] == tool_name and c["input"].get("action")
        }
        coverage_pct = 100 * len(actions & set(wf_actions)) / len(wf_actions)
        finalized = terminal in actions
        refusals = sum(1 for r in trace.tool_results if r.get("is_error"))
    else:
        cov = _phase_coverage_skill(trace, wf_actions)
        coverage_pct = cov["phase_coverage_pct"]
        finalized = False  # SKILL arm has no terminal marker; reflected in coverage
        refusals = 0
    return {
        "arm": trace.arm,
        "model": trace.model,
        "workflow": trace.workflow,
        "track": trace.prompt_track,
        "seed": trace.seed,
        "turns": trace.num_turns or 0,
        "cost_usd": round(trace.cost_usd or 0.0, 3),
        "tool_calls": len(trace.tool_calls),
        "coverage_pct": round(coverage_pct, 1),
        "finalized": finalized,
        "refusals": refusals,
        "is_error": bool(trace.is_error),
    }


def print_table(rows: list[dict]) -> None:
    cols = [
        "arm",
        "model",
        "workflow",
        "track",
        "seed",
        "turns",
        "cost_usd",
        "tool_calls",
        "coverage_pct",
        "finalized",
        "refusals",
    ]
    widths = {c: max(len(c), max((len(str(r[c])) for r in rows), default=0)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--slice",
        choices=("smoke", "default", "launch", "broad"),
        default="default",
        help=(
            "smoke=1x1x1x1, default=1wf*2models*2tracks*1seed, "
            "launch=2wf*2models*2tracks*3seeds (the publish-grade matrix), "
            "broad=2wf*3models*2tracks*2seeds"
        ),
    )
    ap.add_argument("--workflows", nargs="+", choices=list(WORKFLOWS), default=None)
    ap.add_argument("--models", nargs="+", choices=MODELS, default=None)
    ap.add_argument("--tracks", nargs="+", choices=TRACKS, default=None)
    ap.add_argument("--seeds", type=int, default=None)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "bench" / "traces")
    ap.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help=(
            "Max simultaneous cells. Default 4. Lower (1-2) if MCP "
            "server discovery races cause FSM-arm tools to not appear."
        ),
    )
    args = ap.parse_args()

    presets = {
        "smoke": {
            "workflows": ["doc-coauthoring"],
            "models": ["sonnet"],
            "tracks": ["adversarial"],
            "seeds": 1,
        },
        "default": {
            "workflows": ["doc-coauthoring"],
            "models": ["sonnet", "haiku"],
            "tracks": ["cooperative", "adversarial"],
            "seeds": 1,
        },
        "launch": {
            "workflows": ["doc-coauthoring", "security-audit"],
            "models": ["sonnet", "haiku"],
            "tracks": ["cooperative", "adversarial"],
            "seeds": 3,
        },
        "broad": {
            "workflows": list(WORKFLOWS),
            "models": list(MODELS),
            "tracks": list(TRACKS),
            "seeds": 2,
        },
    }
    preset = presets[args.slice]
    workflows = args.workflows or preset["workflows"]
    models = args.models or preset["models"]
    tracks = args.tracks or preset["tracks"]
    seeds = args.seeds if args.seeds is not None else preset["seeds"]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_jsonl = args.out / f"p0_{stamp}.jsonl"
    print(
        f"P0 matrix: {workflows=} {models=} {tracks=} seeds={seeds} arms=['skill','fsm']\n"
        f"Trace output: {out_jsonl}\n"
    )

    cells = [
        {
            "workflow_name": wf,
            "arm": arm,
            "model": m,
            "track": t,
            "seed": s,
            "out_jsonl": out_jsonl,
        }
        for wf in workflows
        for m in models
        for t in tracks
        for s in range(seeds)
        for arm in ("skill", "fsm")
    ]
    sem = asyncio.Semaphore(args.concurrency)

    async def _bounded(c):
        async with sem:
            return await run_cell(**c)

    print(f"Launching {len(cells)} cells (max {args.concurrency} concurrent)...\n")
    traces = await asyncio.gather(*(_bounded(c) for c in cells))

    rows = []
    for t in traces:
        wf = WORKFLOWS[t.workflow]
        rows.append(headline_row(t, wf.fsm_actions, wf.terminal_action))

    print(f"\n{'=' * 72}\nHEADLINE TABLE\n{'=' * 72}")
    print_table(rows)

    print(f"\n{'=' * 72}\nDELTA SUMMARY (FSM coverage_pct - SKILL coverage_pct)\n{'=' * 72}")
    by_key: dict[tuple, dict[str, list[dict]]] = {}
    for r in rows:
        key = (r["workflow"], r["model"], r["track"])
        by_key.setdefault(key, {"skill": [], "fsm": []})[r["arm"]].append(r)
    for (wf, m, t), arms in sorted(by_key.items()):
        s = arms.get("skill", [])
        f = arms.get("fsm", [])
        if not (s and f):
            continue
        s_cov = sum(x["coverage_pct"] for x in s) / len(s)
        f_cov = sum(x["coverage_pct"] for x in f) / len(f)
        s_cost = sum(x["cost_usd"] for x in s) / len(s)
        f_cost = sum(x["cost_usd"] for x in f) / len(f)
        delta = f_cov - s_cov
        marker = "  <-- Theodosia wins" if delta > 10 else "  <-- close" if abs(delta) <= 10 else ""
        print(
            f"  {wf:18} {m:7} {t:12}  "
            f"SKILL cov={s_cov:5.1f}% (${s_cost:.2f})  "
            f"FSM cov={f_cov:5.1f}% (${f_cost:.2f})  "
            f"Δ={delta:+.1f}{marker}"
        )

    json_path = args.out / f"p0_{stamp}_summary.json"
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nFull row data: {json_path}")


if __name__ == "__main__":
    asyncio.run(main())
