"""Function-call safety check + repair loop via Granite Guardian.

A caller LLM (Claude, Sonnet, Granite, whatever drives Theodosia)
proposes a tool / function call. Granite Guardian classifies the
call as safe or unsafe; on unsafe, the FSM loops back so the caller
can revise the call. After ``max_attempts`` unsafe verdicts in a row
the FSM routes to a refused-terminal state, the same retry-as-
transitions pattern as ``granite_oncall.py`` but for safety
classification rather than malformed-output retries.

FSM shape:

    start -> propose_call -> check_safety
        -> finalize_safe      (verdict == "safe")
        -> propose_call       (verdict == "unsafe" and attempts < max)
        -> finalize_refused   (verdict == "unsafe" and attempts == max)

The Guardian call lives behind a monkey-patchable indirection
``_call_guardian`` so tests can supply deterministic verdicts; the
production path calls Granite Guardian via Ollama and parses the
verdict from the classifier output. Set ``BURR_MCP_GUARDIAN_MODEL``
to point at a different Guardian variant.

Pre-req for a real run (tests are hermetic):

    ollama serve &
    ollama pull granite3-guardian:2b
    uv run python examples/granite_guardian.py

A typical session:

    start(operation_description="delete files")
    propose_call(operation="delete_file", args={"path": "/etc/passwd"})
    check_safety()                  # Guardian says unsafe
    propose_call(operation="delete_file", args={"path": "/tmp/scratch.txt"})
    check_safety()                  # Guardian says safe
    finalize_safe()
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "granite-guardian-demo"
_DEFAULT_MODEL = os.environ.get("BURR_MCP_GUARDIAN_MODEL", "granite3-guardian:2b")
_OLLAMA_BASE = os.environ.get("BURR_MCP_GUARDIAN_OLLAMA_BASE", "http://localhost:11434")
_GUARDIAN_TIMEOUT = float(os.environ.get("BURR_MCP_GUARDIAN_TIMEOUT", "30.0"))
_DEFAULT_MAX_ATTEMPTS = 3

_VALID_VERDICTS = {"safe", "unsafe"}


class GuardianUnavailable(RuntimeError):
    """Raised when Ollama is unreachable or the Guardian model is missing."""


async def _call_guardian(
    *,
    operation: str,
    args: dict[str, Any],
    model: str = _DEFAULT_MODEL,
) -> dict[str, Any]:
    """Classify a proposed function call as safe or unsafe.

    Sends a Guardian-shaped prompt to Ollama and parses the verdict.
    The expected response is a short classification (the Granite
    Guardian family returns Yes/No or safe/unsafe tokens) plus an
    optional rationale. Tests monkey-patch this whole function.

    Returns a dict ``{"verdict": "safe"|"unsafe", "reason": str,
    "raw": str}``. Raises ``GuardianUnavailable`` on network errors so
    "Ollama is down" surfaces as an operator-actionable error rather
    than another retry.
    """
    system = (
        "You are Granite Guardian. Classify the following proposed tool "
        "call as safe or unsafe. Respond with exactly one of: 'safe' or "
        "'unsafe', followed by a short reason on the next line."
    )
    body = json.dumps({"operation": operation, "args": args}, indent=2)
    prompt = f"Proposed tool call:\n\n{body}\n\nClassification:"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=_GUARDIAN_TIMEOUT) as client:
            r = await client.post(f"{_OLLAMA_BASE}/api/chat", json=payload)
    except httpx.HTTPError as exc:
        raise GuardianUnavailable(f"Ollama unreachable at {_OLLAMA_BASE}: {exc}") from exc
    if r.status_code != 200:
        raise GuardianUnavailable(f"Ollama returned {r.status_code}: {r.text[:200]}")
    data = r.json()
    raw = (data.get("message", {}).get("content") or "").strip()
    verdict = "unsafe"
    reason = raw
    for line in raw.splitlines():
        token = line.strip().lower().strip(".:")
        if token in ("safe", "yes"):
            verdict = "safe"
            break
        if token in ("unsafe", "no"):
            verdict = "unsafe"
            break
    return {"verdict": verdict, "reason": reason, "raw": raw}


# == FSM actions =====================================================


@action(
    reads=[],
    writes=[
        "operation_description",
        "max_attempts",
        "proposed_calls",
        "verdicts",
        "approved_call",
        "final_outcome",
        "current_prompt",
        "log",
    ],
)
def start(
    state: State,
    operation_description: str,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> State:
    """Open a guarded function-call session.

    Args:
        operation_description: free-text description of what the agent
            is trying to do; shows up in state so an auditor reading
            theodosia://history knows the intent.
        max_attempts: how many unsafe verdicts to accept before
            refusing. Defaults to 3.
    """
    if not operation_description.strip():
        raise ValueError("operation_description must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    prompt = (
        f"You are about to propose a tool call to accomplish: "
        f"{operation_description!r}. Call "
        f"`propose_call(operation='...', args={{...}})` with the "
        f"specific call you'd like to make. Granite Guardian will "
        f"then classify it as safe or unsafe; you have up to "
        f"{max_attempts} attempts to land a safe call before the FSM "
        f"refuses."
    )
    return state.update(
        operation_description=operation_description,
        max_attempts=max_attempts,
        proposed_calls=[],
        verdicts=[],
        approved_call=None,
        final_outcome=None,
        current_prompt=prompt,
        log=[
            f"Session started: operation_description={operation_description!r}, "
            f"max_attempts={max_attempts}"
        ],
    )


@action(
    reads=["proposed_calls", "log"],
    writes=["proposed_calls", "current_prompt", "log"],
)
def propose_call(state: State, operation: str, args: dict[str, Any]) -> State:
    """Record the agent's proposed call. Next: check_safety."""
    if not operation.strip():
        raise ValueError("operation must not be empty")
    attempt_num = len(state["proposed_calls"]) + 1
    entry = {"attempt": attempt_num, "operation": operation, "args": dict(args)}
    return state.update(
        proposed_calls=[*state["proposed_calls"], entry],
        current_prompt=(
            f"Proposal #{attempt_num} recorded: {operation}(...). "
            "Now call `check_safety()` to have Granite Guardian classify "
            "it. Read state.verdicts[-1] after to see the verdict and "
            "the reasoning."
        ),
        log=[*state["log"], f"Proposal #{attempt_num}: {operation}"],
    )


@action(
    reads=["proposed_calls", "verdicts", "max_attempts", "log"],
    writes=["verdicts", "current_prompt", "log"],
)
async def check_safety(state: State) -> State:
    """Call Guardian on the most recent proposal; store the verdict.

    The verdict drives the transition layer; this action does not
    decide outcomes, only stores Guardian's classification.
    """
    if not state["proposed_calls"]:
        raise ValueError("check_safety called without a prior propose_call")
    last = state["proposed_calls"][-1]
    result = await _call_guardian(operation=last["operation"], args=last["args"])
    if result.get("verdict") not in _VALID_VERDICTS:
        raise ValueError(f"_call_guardian returned an invalid verdict: {result.get('verdict')!r}")
    verdict_entry = {
        "attempt": last["attempt"],
        "operation": last["operation"],
        "verdict": result["verdict"],
        "reason": result.get("reason", ""),
        "raw": result.get("raw", ""),
    }
    verdicts = [*state["verdicts"], verdict_entry]
    remaining = state["max_attempts"] - len(verdicts)
    if result["verdict"] == "safe":
        next_prompt = (
            f"Guardian classified attempt #{last['attempt']} as SAFE. "
            "Call `finalize_safe()` to terminate."
        )
    elif remaining > 0:
        next_prompt = (
            f"Guardian classified attempt #{last['attempt']} as UNSAFE. "
            f"Reason: {result.get('reason', '')[:200]} ... "
            f"You have {remaining} attempt(s) remaining. Revise the "
            f"call and try `propose_call(...)` again."
        )
    else:
        next_prompt = (
            f"Guardian classified attempt #{last['attempt']} as UNSAFE. "
            f"No attempts remaining ({state['max_attempts']} exhausted). "
            "Call `finalize_refused()` to record the refusal."
        )
    return state.update(
        verdicts=verdicts,
        current_prompt=next_prompt,
        log=[
            *state["log"],
            f"Guardian verdict #{last['attempt']}: {result['verdict']}",
        ],
    )


@action(
    reads=["proposed_calls", "verdicts", "log"],
    writes=["approved_call", "final_outcome", "current_prompt", "log"],
)
def finalize_safe(state: State) -> State:
    """Terminal: record the approved call and exit."""
    last_proposal = state["proposed_calls"][-1]
    return state.update(
        approved_call=last_proposal,
        final_outcome="approved",
        current_prompt=("Session terminated. Approved call in state.approved_call."),
        log=[*state["log"], f"Approved: {last_proposal['operation']}"],
    )


@action(
    reads=["verdicts", "log"],
    writes=["final_outcome", "current_prompt", "log"],
)
def finalize_refused(state: State) -> State:
    """Terminal: record the refusal and exit."""
    reasons = [v["reason"] for v in state["verdicts"]]
    return state.update(
        final_outcome="refused",
        current_prompt=(
            "Session terminated by Guardian after exhausting attempts. "
            "See state.verdicts for each verdict and Guardian's reasoning."
        ),
        log=[
            *state["log"],
            f"Refused after {len(reasons)} unsafe verdict(s)",
        ],
    )


# == graph ===========================================================


_SAFE = Condition.expr("verdicts and verdicts[-1]['verdict'] == 'safe'")
_UNSAFE_HAS_RETRIES = Condition.expr(
    "verdicts and verdicts[-1]['verdict'] == 'unsafe' and len(verdicts) < max_attempts"
)
_UNSAFE_NO_RETRIES = Condition.expr(
    "verdicts and verdicts[-1]['verdict'] == 'unsafe' and len(verdicts) >= max_attempts"
)


def build_application(max_attempts: int = _DEFAULT_MAX_ATTEMPTS):
    return (
        ApplicationBuilder()
        .with_actions(
            start=start,
            propose_call=propose_call,
            check_safety=check_safety,
            finalize_safe=finalize_safe,
            finalize_refused=finalize_refused,
        )
        .with_transitions(
            ("start", "propose_call"),
            ("propose_call", "check_safety"),
            # Verdict-driven branches out of check_safety.
            ("check_safety", "finalize_safe", _SAFE),
            ("check_safety", "propose_call", _UNSAFE_HAS_RETRIES),
            ("check_safety", "finalize_refused", _UNSAFE_NO_RETRIES),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            operation_description="",
            max_attempts=max_attempts,
            proposed_calls=[],
            verdicts=[],
            approved_call=None,
            final_outcome=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="granite-guardian",
        instructions=(
            "Granite Guardian function-call safety check + repair loop. "
            "Walk: start(operation_description) -> propose_call("
            "operation, args) -> check_safety -> (if safe) finalize_safe "
            "OR (if unsafe + retries) propose_call again OR (if unsafe "
            "+ exhausted) finalize_refused. Each Guardian verdict is "
            f"captured in state.verdicts; max attempts defaults to "
            f"{_DEFAULT_MAX_ATTEMPTS}. The retry loop is encoded as "
            "transitions so every revision is a separate step in "
            "theodosia://history and the audit trail records what Guardian "
            "said about each proposal. Requires Ollama + a Granite "
            f"Guardian model ({_DEFAULT_MODEL}); see the module "
            "docstring for setup. Hermetic via the monkey-patchable "
            "_call_guardian indirection."
        ),
    )


if __name__ == "__main__":
    build_server().run()
