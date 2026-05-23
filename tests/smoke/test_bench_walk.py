"""Parameterized FSM-walk smoke tests for every zero-setup demo in the bench.

For each server in ``~/burrmcp-demo/.mcp.json`` not already covered by a
hand-written smoke test, asks a real Claude session to walk its FSM to
a terminal state. Assertions are intentionally structural: the model
called the server's step tool, made meaningful progress, and the last
call succeeded (or the FSM hit a terminal where ``valid_next_actions``
is empty).

The hypothesis under test: with the polish work (ResourcesAsTools so
Claude can read burr://graph through a tool, ctx.info per step, and the
headline at content[0]), a frontier model can navigate any of these
FSMs unaided. If it can't, the polish isn't doing its job and the
specific failure tells us where to invest.

Cost: ~$2 per demo on the Max plan's Agent SDK credit. A full sweep of
the 22 uncovered demos is roughly $45.

Run explicitly:

    uv run pytest -m smoke tests/smoke/test_bench_walk.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

from ._helpers import (
    actions_called,
    calls_to,
    check_environment_or_skip,
    drive,
    result_for,
)

check_environment_or_skip()


# (server_name, prompt_hint, min_steps).
#
# server_name matches the key in ~/burrmcp-demo/.mcp.json; the tool name
# is "mcp__<server>__step".
#
# prompt_hint is appended to the generic "walk this FSM" preamble. Keep
# it minimal: the demo bench is the source of truth and the model reads
# burr://graph for the action surface. Where the FSM needs concrete
# inputs that aren't obvious (a directory, a corpus, a target file), the
# hint provides them; otherwise the hint is empty.
#
# min_steps is the floor for "Claude meaningfully walked the FSM"; set
# below the demo's known happy-path length so the test isn't brittle.
_CASES: list[tuple[str, str, int]] = [
    ("adventure", "Pick any path; explore until you reach a terminal room.", 3),
    ("async-hooks", "", 2),
    ("async-persister", "", 2),
    (
        "burr-shell",
        "Once in the sandbox, list its files and read one of them.",
        2,
    ),
    (
        "burr-unix-health",
        "Run disk, memory, and load checks; then write a summary.",
        3,
    ),
    ("chargen", "Build a character: pick a class, race, and one skill.", 3),
    ("class-action", "", 2),
    (
        "codebase-security",
        "Scan the shipped vuln_demo directory; report what you find.",
        3,
    ),
    ("combo-testing", "Run a sweep of at least 3 parameter combinations.", 3),
    # custom-telemetry, parallel-research, streaming-hooks, and
    # trace-decorator are single-action FSMs: with_actions(X=X) where
    # X does all the substantive work internally (spans, asyncio.gather,
    # streaming, @trace helpers). One step call IS the workflow.
    ("custom-telemetry", "", 1),
    ("full-logger", "", 2),
    ("ml-training", "Train until convergence or the FSM terminates.", 3),
    (
        "parallel-research",
        "Pick any topic in the shipped corpus and run the research.",
        1,
    ),
    ("pipeline-hooks", "", 2),
    ("pydantic-actions", "", 2),
    (
        "state-forking",
        "Plan a budget, then exercise the fork to try an alternate.",
        3,
    ),
    ("streaming-hooks", "Pick any topic and run the streaming pipeline.", 1),
    (
        "streaming-narrate",
        "Pick a topic like 'wizard' or 'knight' and run narrate.",
        1,
    ),
    ("subgraph-composition", "", 2),
    ("trace-decorator", "", 1),
    ("typed-state-loan", "Apply for a small loan; walk to approval or denial.", 3),
    ("with-middleware", "", 2),
]


def _preamble(server: str, hint: str) -> str:
    base = (
        f"Use the {server} burr app to walk its FSM to a terminal state. "
        f"Start by calling read_resource for `burr://graph` (or list_resources "
        f"if you need to see what's there) to learn the actions and "
        f"transitions. Then call the {server} step tool repeatedly, one "
        f"action at a time, in the order the graph allows. Each step "
        f"response carries `valid_next_actions`; use that to pick the next "
        f"call. Provide reasonable inputs where an action requires them. "
        f"Don't ask me for anything; invent sensible values yourself. Stop "
        f"as soon as `valid_next_actions` is empty (terminal). Don't write "
        f"a summary afterward."
    )
    return f"{base} {hint}".strip() if hint else base


@pytest.mark.parametrize(("server", "hint", "min_steps"), _CASES, ids=[c[0] for c in _CASES])
@pytest.mark.asyncio
async def test_bench_demo_walks_to_terminal(server: str, hint: str, min_steps: int):
    tool_name = f"mcp__{server}__step"
    trace = await drive(
        _preamble(server, hint),
        max_budget_usd=5.0,
        max_turns=40,
    )

    step_calls = calls_to(trace["tool_calls"], tool_name)
    assert step_calls, (
        f"Claude never called {tool_name}. Tools called: "
        f"{sorted({c['name'] for c in trace['tool_calls']})}"
    )

    assert len(step_calls) >= min_steps, (
        f"Only {len(step_calls)} step calls (expected >={min_steps}). "
        f"Actions: {actions_called(trace['tool_calls'], tool_name)!r}"
    )

    last_call = step_calls[-1]
    last_result = result_for(trace["tool_results"], last_call["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None, f"Last {tool_name} call had no parsed payload: {last_result!r}"

    # Either the FSM walked to terminal (success, no next actions) or
    # the last call succeeded mid-walk. Either is fine; an error refusal
    # on the LAST call would mean the model gave up at a refusal.
    last_error = parsed.get("error")
    last_next = parsed.get("valid_next_actions")
    reached_terminal = last_error is None and last_next == []
    last_succeeded = last_error is None
    assert reached_terminal or last_succeeded, (
        f"Last {tool_name} call failed and wasn't terminal: {parsed!r}\n"
        f"Actions walked: {actions_called(trace['tool_calls'], tool_name)!r}"
    )
