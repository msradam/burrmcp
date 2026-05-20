"""Records README's demo.gif via vhs.

Drives a real Claude session through the Agent SDK against the demo
bench (``~/burrmcp-demo/.mcp.json``). The user gives one
natural-language instruction; Claude figures out which MCP tool to
call and walks the incident_response FSM end-to-end.

The script prints a compact, color-coded conversation: user prompt,
then each tool call Claude made and the structured response from
the server. Shows the actual property of burrmcp: agents drive the
state machine in plain English; the server enforces the order.

Cost: ~$0.05 per run on Claude Max (Sonnet via SDK). Re-record the
GIF only when the demo's shape changes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests" / "smoke"))

# Reuse the smoke harness so this script and the smoke suite stay aligned.
from _helpers import check_environment_or_skip, drive  # noqa: E402

check_environment_or_skip()


PROMPT = """\
Use the incident-response MCP server. Walk the incident end-to-end:

1. step(action='report')           # parses the shipped alert.json
2. step(action='acknowledge', responder='claude')
3. step(action='investigate')      # spawns the investigation sub-app
4. step(action='mitigate', action_kind='rollback', target='v2.14.2',
        simulated_offset_minutes=9)
5. step(action='verify', verified=true, notes='clean post-rollback')
6. step(action='resolve', resolution='rolled back v2.14.3 -> v2.14.2')

Don't explain. Just call the tools."""


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _summarise_result(parsed: dict | None) -> str:
    """Return a one-line summary of an MCP step response.

    Picks the field that's most informative for the action that just
    ran, rather than always returning the same persistent state field.
    """
    if parsed is None:
        return ""
    if parsed.get("error"):
        return _c("31", f"✗ {parsed['error']}: valid_next={parsed.get('valid_next_actions')}")
    state = parsed.get("state") or {}
    action = parsed.get("action") or ""
    if action == "report":
        return _c(
            "32",
            f"✓ parsed alert: service={state.get('service')} severity={state.get('severity')} "
            f"pod={state.get('pod')}",
        )
    if action == "acknowledge":
        return _c("32", f"✓ acknowledged by {state.get('responder')}")
    if action == "investigate":
        hyp = (state.get("hypothesis") or "").splitlines()[0]
        if len(hyp) > 90:
            hyp = hyp[:88] + "…"
        return _c("32", f"✓ root cause: {hyp}")
    if action == "mitigate":
        mit = state.get("mitigation") or {}
        return _c("32", f"✓ recorded: {mit.get('action_kind')} -> {mit.get('target')}")
    if action == "verify":
        ev = state.get("verification_evidence") or {}
        return _c(
            "32",
            f"✓ verified: {ev.get('error_count', '?')} ERROR lines in "
            f"{ev.get('lines_inspected', '?')} post-mitigation lines",
        )
    if action == "resolve":
        return _c("32", f"✓ resolved: {state.get('resolution')}")
    return _c("32", "✓ ok")


async def main():
    print(_c("1;36", "─" * 78))
    print(_c("1;33", "🧑 user:"))
    for line in PROMPT.splitlines():
        print(f"   {line}")
    print(_c("1;36", "─" * 78))
    print()

    trace = await drive(PROMPT, max_budget_usd=2.0, max_turns=15)

    # Pair each tool_use with its tool_result by id and print them in order.
    results_by_id = {r["tool_use_id"]: r for r in trace["tool_results"]}
    for call in trace["tool_calls"]:
        name = call["name"]
        if not name.startswith("mcp__incident-response__"):
            continue
        action = call["input"].get("action", "?")
        inputs = {k: v for k, v in call["input"].get("inputs", {}).items() if k != "ctx"}
        print(_c("1;35", "🤖 claude →"), _c("36", f"step(action={action!r})"))
        if inputs:
            print(f"           inputs={inputs}")
        result = results_by_id.get(call["id"])
        if result is not None:
            print(f"   {_summarise_result(result.get('parsed'))}")
        print()

    print(_c("1;36", "─" * 78))
    cost = trace["result"].total_cost_usd if trace.get("result") else 0.0
    print(_c("2", f"   four MCP tools; agent walks the FSM in plain English. cost: ${cost:.3f}"))
    print()


if __name__ == "__main__":
    asyncio.run(main())
