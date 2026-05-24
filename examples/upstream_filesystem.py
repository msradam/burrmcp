"""upstream: a code-audit FSM that drives a filesystem MCP server THROUGH
burrmcp (single surface).

Unlike ``external_tools_filesystem`` (advisory: the agent calls the
filesystem server itself), here burrmcp is an MCP *client* to the
filesystem server. The agent connects to ONLY this burrmcp server and
sees ONLY the ``step`` tool. The audit actions call the filesystem
server's tools from inside their bodies via ``call_upstream(...)``. The
filesystem tools are never exposed to the agent.

This is the honest "works with any MCP server" path: burrmcp is in the
call path, every upstream read happens inside an action (so it advances
state), and it works with any compliant MCP server because fastmcp.Client
speaks every transport.

Audit target dir: ``$AUDIT_TARGET`` (defaults to the shipped vuln_demo).

Run:

    AUDIT_TARGET=/path/to/code \\
      burrmcp serve upstream_filesystem:build_server --name code-audit

Then connect an agent to ONLY this server. It audits real files without
ever being given filesystem tools.
"""

from __future__ import annotations

import os
from pathlib import Path

from burr.core import ApplicationBuilder, Condition, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, call_upstream, mount

_TRACKER_PROJECT = "upstream-code-audit-demo"
_MIN_FINDINGS = 2
_DEFAULT_TARGET = str(Path(__file__).parent / "data" / "codebase_security" / "vuln_demo")


def _audit_target() -> str:
    return os.environ.get("AUDIT_TARGET", _DEFAULT_TARGET)


@action(reads=[], writes=["target", "phase", "tree", "reads", "findings", "log"])
async def start_audit(state: State) -> State:
    """Open the audit and survey the tree via the filesystem server."""
    target = _audit_target()
    tree = await call_upstream("filesystem", "directory_tree", {"path": target})
    return state.update(
        target=target,
        phase="inspect",
        tree=tree,
        reads=[],
        findings=[],
        log=[f"audit opened: {target}"],
    )


@action(reads=["target", "reads", "log"], writes=["reads", "last_content", "log"])
async def read_file(state: State, path: str) -> State:
    """Read a file via the filesystem server. Its content is returned to
    you in the step result so you can judge it, and the read is recorded.

    Args:
        path: absolute path to read (within the audit target).
    """
    if not path.strip():
        raise ValueError("path must not be empty")
    content = await call_upstream("filesystem", "read_file", {"path": path.strip()})
    text = content if isinstance(content, str) else str(content)
    return state.update(
        reads=[*state["reads"], path.strip()],
        last_content=text[:4000],
        log=[*state["log"], f"read {path.strip()}"],
    )


@action(reads=["reads", "findings", "log"], writes=["findings", "log"])
async def record_finding(state: State, path: str, finding: str, severity: str = "info") -> State:
    """Record a security finding after reading a file.

    Args:
        path: the file the finding is about.
        finding: what's wrong (or "no issues found").
        severity: critical | high | medium | low | info.
    """
    if not path.strip() or not finding.strip():
        raise ValueError("path and finding must both be non-empty")
    if path.strip() not in state["reads"]:
        raise ValueError(
            f"record a finding only for a file you read first; you have not "
            f"read {path.strip()!r}. Call read_file on it before recording."
        )
    sev = severity.strip().lower()
    if sev not in {"critical", "high", "medium", "low", "info"}:
        raise ValueError("severity must be one of critical/high/medium/low/info")
    return state.update(
        findings=[
            *state["findings"],
            {"path": path.strip(), "finding": finding.strip(), "severity": sev},
        ],
        log=[*state["log"], f"finding {path.strip()}: {sev}"],
    )


@action(reads=["target", "findings", "log"], writes=["phase", "advisory", "log"])
async def write_advisory(state: State, advisory: str) -> State:
    """Terminal. Compile the advisory. Requires >=2 recorded findings."""
    if len(state["findings"]) < _MIN_FINDINGS:
        raise ValueError(
            f"write_advisory requires >= {_MIN_FINDINGS} findings; you have "
            f"{len(state['findings'])}. Read + record more files."
        )
    if len(advisory.strip()) < 80:
        raise ValueError("advisory must be a substantive markdown report (>=80 chars)")
    return state.update(
        phase="done",
        advisory=advisory.strip(),
        log=[*state["log"], f"advisory written ({len(state['findings'])} findings)"],
    )


_OPEN = Condition.expr("phase != 'done'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_audit=start_audit,
            read_file=read_file,
            record_finding=record_finding,
            write_advisory=write_advisory,
        )
        .with_transitions(
            ("start_audit", "read_file", _OPEN),
            ("read_file", "read_file", _OPEN),
            ("read_file", "record_finding", _OPEN),
            ("record_finding", "read_file", _OPEN),
            ("record_finding", "record_finding", _OPEN),
            ("record_finding", "write_advisory", _OPEN),
            ("read_file", "write_advisory", _OPEN),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            target="",
            phase="new",
            tree=None,
            reads=[],
            last_content=None,
            findings=[],
            advisory=None,
            log=[],
        )
        .with_entrypoint("start_audit")
        .build()
    )


def build_server():
    target = _audit_target()
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="code-audit",
        upstream={
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", target],
            }
        },
        instructions=(
            "A source-code security-audit FSM that drives a filesystem MCP "
            "server THROUGH this server (you are not given filesystem tools "
            "directly). Walk: start_audit() surveys the tree; read_file(path) "
            "returns a file's content; record_finding(path, finding, severity) "
            "logs an issue (read the file first); write_advisory(advisory) "
            "finishes (needs >=2 findings). Read state.current_prompt / "
            "state.last_content after each step."
        ),
    )


if __name__ == "__main__":
    build_server().run()
