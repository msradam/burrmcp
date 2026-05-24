"""external_tools, second domain: a code-audit FSM that orchestrates the
official Filesystem MCP server.

Like ``external_tools_k8s`` but for a completely different domain (files,
not clusters) to show the federation pattern generalizes. This FSM holds
no file-reading tools of its own. Each action declares which tools on the
SEPARATE ``@modelcontextprotocol/server-filesystem`` server are relevant
when that action is the next move. The agent reads ``next_external_tools``
off each step response, calls those filesystem tools, then ``step()``s
here to record findings and advance.

Shape:

    start_audit --> survey_tree --> inspect_file (loop, >=2 files) --> write_advisory

Per-phase external filesystem tools (names match the official server):

    start_audit:    list_allowed_directories, directory_tree
    survey_tree:    directory_tree, list_directory, search_files
    inspect_file:   read_file, read_multiple_files, search_files, get_file_info
    write_advisory: (none; you've gathered the evidence, now conclude)

Run:

    burrmcp serve external_tools_filesystem:build_application --name code-audit
    # then connect an agent to BOTH this server and a filesystem MCP server:
    #   npx -y @modelcontextprotocol/server-filesystem <audit-target-dir>
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, Condition, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "code-audit-demo"
_MIN_FILES = 2


@action(reads=[], writes=["target", "phase", "tree_summary", "findings", "log"])
async def start_audit(state: State, target: str) -> State:
    """Open a source-code security audit.

    Args:
        target: the directory under audit (the filesystem MCP server must
            be allowed to read it).
    """
    if not target.strip():
        raise ValueError("target must not be empty")
    return state.update(
        target=target.strip(),
        phase="survey",
        tree_summary="",
        findings=[],
        log=[f"audit opened: {target.strip()}"],
    )


@action(reads=["phase", "log"], writes=["tree_summary", "phase", "log"])
async def survey_tree(state: State, structure_summary: str) -> State:
    """Record the directory structure you observed via the filesystem
    server (directory_tree / list_directory).

    Args:
        structure_summary: a short description of the files / layout worth
            auditing (which files exist, what they seem to do).
    """
    if len(structure_summary.strip()) < 20:
        raise ValueError(
            "structure_summary too thin; use the filesystem server's "
            "directory_tree / list_directory first, then summarize what's there."
        )
    return state.update(
        tree_summary=structure_summary.strip(),
        phase="inspect",
        log=[*state["log"], "surveyed tree"],
    )


@action(reads=["phase", "findings", "log"], writes=["findings", "log"])
async def inspect_file(state: State, path: str, finding: str, severity: str = "info") -> State:
    """Record one finding from inspecting a file (read via the filesystem
    server's read_file / search_files). Loop: call once per file of interest.

    Args:
        path: the file you inspected.
        finding: what you found (or "no issues found").
        severity: critical | high | medium | low | info.
    """
    if not path.strip() or not finding.strip():
        raise ValueError("path and finding must both be non-empty")
    sev = severity.strip().lower()
    if sev not in {"critical", "high", "medium", "low", "info"}:
        raise ValueError("severity must be one of critical/high/medium/low/info")
    findings = [
        *state["findings"],
        {"path": path.strip(), "finding": finding.strip(), "severity": sev},
    ]
    return state.update(
        findings=findings,
        log=[*state["log"], f"inspected {path.strip()}: {sev}"],
    )


@action(reads=["target", "findings", "log"], writes=["phase", "advisory", "log"])
async def write_advisory(state: State, advisory: str) -> State:
    """Terminal. Compile the findings into an advisory. Requires that you
    inspected at least {_MIN_FILES} files first.

    Args:
        advisory: the full markdown advisory.
    """
    if len(state["findings"]) < _MIN_FILES:
        raise ValueError(
            f"write_advisory requires inspecting at least {_MIN_FILES} files first; "
            f"you have {len(state['findings'])}. Read more files via the filesystem server."
        )
    if len(advisory.strip()) < 80:
        raise ValueError("advisory must be a substantive markdown report (>=80 chars)")
    return state.update(
        phase="done",
        advisory=advisory.strip(),
        log=[*state["log"], f"advisory written ({len(state['findings'])} findings)"],
    )


EXTERNAL_TOOLS = {
    "start_audit": ["list_allowed_directories", "directory_tree"],
    "survey_tree": ["directory_tree", "list_directory", "search_files"],
    "inspect_file": ["read_file", "read_multiple_files", "search_files", "get_file_info"],
    # write_advisory: no external tools; conclude from gathered evidence.
}

_OPEN = Condition.expr("phase != 'done'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_audit=start_audit,
            survey_tree=survey_tree,
            inspect_file=inspect_file,
            write_advisory=write_advisory,
        )
        .with_transitions(
            ("start_audit", "survey_tree", _OPEN),
            ("survey_tree", "inspect_file", _OPEN),
            ("inspect_file", "inspect_file", _OPEN),
            ("inspect_file", "write_advisory", _OPEN),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            target="",
            phase="new",
            tree_summary="",
            findings=[],
            advisory=None,
            log=[],
        )
        .with_entrypoint("start_audit")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="code-audit",
        external_tools=EXTERNAL_TOOLS,
        instructions=(
            "A source-code security-audit FSM that orchestrates a SEPARATE "
            "filesystem MCP server. This server holds no file tools; each step "
            "response carries next_external_tools telling you which tools on the "
            "connected filesystem MCP server to call. Walk: start_audit(target) "
            "-> survey_tree(structure_summary) -> inspect_file(path, finding, "
            "severity) [>=2 files] -> write_advisory(advisory). For each phase, "
            "call the filesystem tools named in next_external_tools (directory_tree, "
            "read_file, search_files, ...), then step() here to record findings."
        ),
    )


if __name__ == "__main__":
    build_server().run()
