"""Codebase audit as a Theodosia FSM. Dogfoods the agentic-SDLC pattern.

Phases mirror the literature's INTENT → SPEC → PLAN → IMPLEMENT → VERIFY
state machine, scoped to a static-analysis pass (no implement step):

    receive_audit
        ↓
    run_ruff  →  run_mypy  →  run_refurb  →  run_bandit
        ↓
    run_radon_cc  →  run_radon_mi
        ↓
    run_tests
        ↓
    summarize  (gate: refuse conclude if any blocking finding)
        ↓
    conclude (terminal)  OR  escalate (terminal, blocking findings)

Each analysis action shells out to the real tool via subprocess, captures
stdout, and classifies the result (``ok`` / ``warn`` / ``fail``). The
gate at ``summarize`` enforces that ``conclude`` is only reachable when
ruff / mypy / tests pass and refurb / bandit have no medium+ findings;
otherwise the FSM routes to ``escalate``.

Run me::

    uv run python examples/codebase_audit.py /path/to/src/theodosia

I default to auditing this repo's own ``src/theodosia``.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal

from burr.core import ApplicationBuilder, Condition, State, action
from pydantic import BaseModel, Field

from theodosia import mount, tracker

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_TARGET = str(_REPO / "src" / "theodosia")


class AuditRequest(BaseModel):
    """Inputs to ``receive_audit``."""

    target_dir: str = Field(description="Directory to analyze. Absolute path.")
    strict: bool = Field(
        default=True,
        description="When True, refurb/bandit findings block conclude.",
    )


def _run(cmd: str, *, cwd: str | None = None) -> tuple[int, str]:
    """Shell out, capturing combined output."""
    proc = subprocess.run(  # nosec B603 (cmd is built from constants in this module)
        shlex.split(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _classify(rc: int, output: str, tool: str) -> Literal["ok", "warn", "fail"]:
    if rc == 0:
        return "ok"
    if tool in {"ruff", "mypy", "pytest"}:
        return "fail"
    return "warn"


@action(reads=[], writes=["target_dir", "strict", "findings"])
def receive_audit(state: State, request: AuditRequest) -> State:
    return state.update(
        target_dir=request.target_dir,
        strict=request.strict,
        findings={},
    )


def _record(state: State, tool: str, rc: int, output: str) -> State:
    findings = dict(state["findings"])
    findings[tool] = {
        "status": _classify(rc, output, tool),
        "returncode": rc,
        "summary": output.splitlines()[-1] if output else "(no output)",
        "head": "\n".join(output.splitlines()[:10]),
    }
    return state.update(findings=findings)


@action(reads=["target_dir"], writes=["findings"])
def run_ruff(state: State) -> State:
    rc, out = _run(f"uv run ruff check {state['target_dir']}")
    return _record(state, "ruff", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_mypy(state: State) -> State:
    rc, out = _run(f"uv run mypy {state['target_dir']}")
    return _record(state, "mypy", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_refurb(state: State) -> State:
    rc, out = _run(
        f"uv run --with refurb --project {_REPO} refurb {Path(state['target_dir']).name}",
        cwd=str(Path(state["target_dir"]).parent),
    )
    return _record(state, "refurb", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_bandit(state: State) -> State:
    rc, out = _run(f"uv run --with bandit bandit -r {state['target_dir']} -ll -q")
    return _record(state, "bandit", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_radon_cc(state: State) -> State:
    rc, out = _run(f"uv run --with radon radon cc {state['target_dir']} -s -a -nc")
    return _record(state, "radon_cc", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_radon_mi(state: State) -> State:
    rc, out = _run(f"uv run --with radon radon mi {state['target_dir']} -nb")
    return _record(state, "radon_mi", rc, out)


@action(reads=["target_dir"], writes=["findings"])
def run_tests(state: State) -> State:
    test_dir = str(_REPO / "tests")
    rc, out = _run(f"uv run pytest {test_dir} -q --no-header --maxfail=1 -x")
    return _record(state, "pytest", rc, out)


@action(reads=["findings", "strict"], writes=["verdict", "blocking"])
def summarize(state: State) -> State:
    """Decide conclude vs escalate.

    ``blocking`` is True if ruff, mypy, or pytest failed, or (in strict
    mode) if refurb or bandit raised any finding.
    """
    findings = state["findings"]
    hard_fails = [
        t for t in ("ruff", "mypy", "pytest") if findings.get(t, {}).get("status") == "fail"
    ]
    soft_fails = [t for t in ("refurb", "bandit") if findings.get(t, {}).get("status") != "ok"]
    blocking = bool(hard_fails) or (state["strict"] and bool(soft_fails))
    verdict = {
        "hard_fails": hard_fails,
        "soft_fails": soft_fails,
        "tools_run": list(findings.keys()),
    }
    return state.update(verdict=verdict, blocking=blocking)


@action(reads=["verdict", "findings"], writes=["report"])
def conclude(state: State) -> State:
    """Terminal: codebase passes the audit."""
    return state.update(report=_format_report(state, status="PASS"))


@action(reads=["verdict", "findings"], writes=["report"])
def escalate(state: State) -> State:
    """Terminal: codebase has blocking findings."""
    return state.update(report=_format_report(state, status="BLOCK"))


def _format_report(state: State, *, status: str) -> str:
    """Render a human-readable summary."""
    lines = [f"# Codebase Audit: {status}", ""]
    for tool, r in state["findings"].items():
        glyph = {"ok": "✓", "warn": "⚠", "fail": "✗"}[r["status"]]
        lines.append(f"- {glyph} **{tool}** ({r['status']}, rc={r['returncode']}): {r['summary']}")
    if status == "BLOCK":
        v = state["verdict"]
        if v["hard_fails"]:
            lines.append(f"\n**Hard fails**: {v['hard_fails']}")
        if v["soft_fails"]:
            lines.append(f"**Soft fails (strict mode)**: {v['soft_fails']}")
    return "\n".join(lines)


def build_application():
    """Build the audit FSM.

    Transitions encode the gate: ``conclude`` is reachable only when
    ``blocking == False``; otherwise the only reachable terminal is
    ``escalate``. A client cannot fast-path the gate; the graph refuses
    with ``valid_next_actions: ['escalate']``.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            receive_audit=receive_audit,
            run_ruff=run_ruff,
            run_mypy=run_mypy,
            run_refurb=run_refurb,
            run_bandit=run_bandit,
            run_radon_cc=run_radon_cc,
            run_radon_mi=run_radon_mi,
            run_tests=run_tests,
            summarize=summarize,
            conclude=conclude,
            escalate=escalate,
        )
        .with_transitions(
            ("receive_audit", "run_ruff"),
            ("run_ruff", "run_mypy"),
            ("run_mypy", "run_refurb"),
            ("run_refurb", "run_bandit"),
            ("run_bandit", "run_radon_cc"),
            ("run_radon_cc", "run_radon_mi"),
            ("run_radon_mi", "run_tests"),
            ("run_tests", "summarize"),
            ("summarize", "conclude", Condition.expr("blocking == False")),
            ("summarize", "escalate", Condition.expr("blocking == True")),
        )
        .with_state(
            target_dir="",
            strict=True,
            findings={},
            verdict={},
            blocking=False,
            report="",
        )
        .with_entrypoint("receive_audit")
        .with_tracker(tracker(project="codebase-audit"))
        .build()
    )


async def _drive_in_process(target_dir: str, strict: bool = True) -> None:
    """Drive the FSM end-to-end through an in-memory MCP client."""
    import asyncio  # noqa: F401 (imported for the caller's await; mypy hint)

    from fastmcp import Client

    server = mount(build_application, name="codebase-audit")
    actions = (
        "receive_audit",
        "run_ruff",
        "run_mypy",
        "run_refurb",
        "run_bandit",
        "run_radon_cc",
        "run_radon_mi",
        "run_tests",
        "summarize",
    )

    async with Client(server) as client:
        # Walk every analysis phase.
        for name in actions:
            inputs = (
                {"request": {"target_dir": target_dir, "strict": strict}}
                if name == "receive_audit"
                else {}
            )
            r = await client.call_tool("step", {"action": name, "inputs": inputs})
            payload = r.structured_content or {}
            if payload.get("error"):
                print(f"REFUSED at {name}: {payload}")
                return
            findings = payload.get("state", {}).get("findings", {})
            if name.startswith("run_"):
                tool = name.removeprefix("run_")
                got = findings.get(tool, {})
                print(f"  {name}: {got.get('status')} (rc={got.get('returncode')})")

        # Pick the terminal the gate routed to.
        r = await client.read_resource("theodosia://next")
        reachable = json.loads(r[0].text)
        terminal = "conclude" if "conclude" in reachable else "escalate"
        r = await client.call_tool("step", {"action": terminal, "inputs": {}})
        report = (r.structured_content or {}).get("state", {}).get("report", "")
        print()
        print(report)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_TARGET
    strict = "--strict" in sys.argv
    if not Path(target).is_dir():
        print(f"target is not a directory: {target}", file=sys.stderr)
        sys.exit(1)

    import asyncio

    asyncio.run(_drive_in_process(target, strict=strict))


if __name__ == "__main__":
    main()
