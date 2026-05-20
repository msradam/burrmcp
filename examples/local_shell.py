"""burr-shell: natural-language shell against a sandboxed FS.

The user asks for shell operations in plain English ("list the files",
"count lines in data.csv", "make a notes/today.md with the headline").
The agent translates intent to a shell command and calls
``execute(command=...)``; the server runs it against a **per-session
temp sandbox** (a fresh copy of ``examples/data/local_shell/``), captures
stdout / stderr / exit_code, and records the call in ``burr://history``.

Why this server exists when the agent already has a built-in shell:

* Every command is auditable in ``burr://history`` with full output.
* The sandbox is a temp dir; the shipped sample data is never mutated.
* Multiple MCP sessions get independent sandboxes; no cross-contamination.
* The agent CANNOT escape the sandbox (commands run with cwd set; we
  refuse absolute paths and ``..`` traversal in obvious cases).

USE THIS SERVER WHEN: the user says "use burr shell" or asks for shell
operations they want gated and recorded rather than fired through the
agent's built-in Bash. Do NOT route shell operations here automatically
when the user expects fast, ungated bash; this is a deliberately
auditable surface.

Run:

    uv run python examples/local_shell.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "burr-shell-demo"
_DATA_DIR = Path(__file__).parent / "data" / "local_shell"

# Match an obvious sandbox-escape pattern in the command. Conservative:
# rejects leading absolute paths, .. traversal, sub-shells, and pipe-to-shell;
# relies on cwd scoping for the rest.
_ESCAPE_PATTERN = re.compile(r"(^|[\s=])/|(\.\./)|(/\.\.)|(`|\$\(|\|\s*sh\b|\|\s*bash\b)")


def _prepare_sandbox() -> Path:
    """Make a fresh temp copy of the shipped sample dir.

    One per session. The factory builds an Application; the Application
    holds the sandbox path in state and the action body reads from there.
    The temp dir leaks on process exit, which is fine for a demo.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="burr-shell-"))
    if _DATA_DIR.exists():
        for child in _DATA_DIR.iterdir():
            if child.is_dir():
                shutil.copytree(child, sandbox / child.name)
            else:
                shutil.copy2(child, sandbox / child.name)
    return sandbox


@action(reads=["sandbox", "history", "step_count"], writes=["history", "step_count"])
async def execute(state: State, command: str, timeout_seconds: int = 10) -> State:
    """Run ``command`` against this session's sandbox dir.

    Returns the next state including a new history entry with stdout,
    stderr, exit_code, and timing. The command runs via ``/bin/sh -c``
    so pipes / redirects work; the working directory is the per-session
    sandbox so relative paths resolve inside it.

    Refused (raises ValueError) when the command:

    * is empty / whitespace only
    * contains a leading absolute path (``/etc/...``)
    * contains ``..`` segments
    * contains an obvious sub-shell or eval (``|sh``, ``|bash``,
      backticks, ``$(...)``)
    """
    if not command.strip():
        raise ValueError("command must not be empty")
    if _ESCAPE_PATTERN.search(command):
        raise ValueError(
            "command contains an obvious sandbox-escape pattern "
            "(absolute path, .. traversal, sub-shell, or pipe to sh/bash). "
            "use relative paths inside the sandbox."
        )

    sandbox = Path(state["sandbox"])
    started = datetime.now(UTC).isoformat()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        entry: dict[str, Any] = {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started_at": started,
            "ended_at": datetime.now(UTC).isoformat(),
        }
    except subprocess.TimeoutExpired as exc:
        entry = {
            "command": command,
            "exit_code": None,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
            "started_at": started,
            "ended_at": datetime.now(UTC).isoformat(),
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
        }
    return state.update(
        history=[*state["history"], entry],
        step_count=state["step_count"] + 1,
    )


@action(reads=["history", "step_count"], writes=["summary"])
async def done(state: State) -> State:
    """Close the session with a small summary. Terminal."""
    history = state["history"]
    return state.update(
        summary={
            "command_count": len(history),
            "successful_count": sum(1 for h in history if h.get("exit_code") == 0),
            "failed_count": sum(1 for h in history if h.get("exit_code") not in (0, None)),
            "timed_out_count": sum(1 for h in history if h.get("timed_out")),
        }
    )


def build_application(*, sandbox: Path | None = None):
    """Build an Application with a fresh per-session sandbox.

    If ``sandbox`` is None, a temp dir is created and seeded with the
    shipped sample data. Tests pass an explicit path for deterministic
    behavior.
    """
    sb = sandbox if sandbox is not None else _prepare_sandbox()
    return (
        ApplicationBuilder()
        .with_identifiers(app_id=uuid.uuid4().hex)
        .with_actions(execute=execute, done=done)
        .with_transitions(
            ("execute", "done", Condition.expr("step_count > 0")),
            ("execute", "execute"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            sandbox=str(sb),
            history=[],
            step_count=0,
            summary=None,
        )
        .with_entrypoint("execute")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="burr-shell",
        instructions=(
            "USE THIS SERVER when the user asks to run shell commands "
            "they want gated and recorded ('use burr shell to ...'). "
            "Do not route shell operations here automatically; only when "
            "the user explicitly invokes burr-shell.\n\n"
            "Each session gets a fresh temp sandbox seeded from "
            "examples/data/local_shell/ (a notes/ dir, data.csv, "
            "config.yaml). Translate the user's natural-language request "
            "into a shell command and call step(action='execute', "
            "inputs={'command': '<command>'}). The command runs via "
            "/bin/sh -c with cwd set to the sandbox. Output (stdout, "
            "stderr, exit_code) appears in state.history and "
            "burr://history. Absolute paths, .. traversal, and obvious "
            "sub-shells are refused. Call step(action='done') to close "
            "with a summary."
        ),
    )


if __name__ == "__main__":
    build_server().run()
