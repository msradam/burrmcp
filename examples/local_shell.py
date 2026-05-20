"""A miniature local-shell agent with safety rails enforced by the FSM.

The model can list files, read them, stage edits, run tests, commit,
and delete files. Safety rules that an agent would otherwise have to
remember in its prompt are encoded as FSM gates the server enforces:

* You can't ``edit_file`` a path you haven't ``read_file``'d first.
  (The whole "must read before write" rule that Claude Code itself
  enforces.)
* You can't ``commit`` unless there are pending edits *and* the last
  test run passed. Editing a file invalidates the previous test
  result.
* You can't ``confirm_delete`` without first calling ``request_delete``
  for that path. Two-step destructive operation.

None of these rules are documented in the agent's prompt. The agent
just calls tools; the server refuses unsafe sequences with an
``invalid_transition`` error (transition-level rules) or an
``action_error`` with the precondition spelled out (action-level
rules).

Try prompting your MCP client with:

  "Edit main.py to print 'goodbye' instead of 'hello'."

A naive agent will reach for ``edit_file`` first. The server refuses,
the agent realises it needs to ``read_file`` first, then ``edit_file``
succeeds. The whole "read-before-write" dance is FSM-enforced, not
prompt-engineered.

Then try:

  "Just commit my edit, skip the tests."

The server refuses until ``run_tests`` has been called and reported
"passed" since the last edit.

Run:

    python examples/local_shell.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "local-shell-demo"

_INITIAL_WORKSPACE = {
    "main.py": 'print("hello")\n',
    "utils.py": "def helper():\n    return 42\n",
    "README.md": "# Local Shell Demo\n",
}


# ── actions ─────────────────────────────────────────────────────────


@action(reads=["workspace", "log"], writes=["log"])
def list_files(state: State) -> State:
    """List the files currently in the workspace. Always callable."""
    paths = sorted(state["workspace"].keys())
    log = [*state.get("log", []), f"Listed {len(paths)} file(s): {paths}."]
    return state.update(log=log)


@action(reads=["workspace", "files_read", "log"], writes=["files_read", "log"])
def read_file(state: State, path: str) -> State:
    """Read a file from the workspace. Records the read in ``files_read``
    so subsequent ``edit_file`` calls on the same path are permitted.

    Args:
        path: Workspace-relative filename, e.g. ``main.py``.
    """
    workspace = state["workspace"]
    if path not in workspace:
        raise ValueError(f"no such file {path!r}. Workspace: {sorted(workspace)}")
    files_read = state.get("files_read", [])
    if path not in files_read:
        files_read = [*files_read, path]
    log = [*state.get("log", []), f"Read {path} ({len(workspace[path])} bytes)."]
    return state.update(files_read=files_read, log=log)


@action(
    reads=["workspace", "pending_edits", "files_read", "log"],
    writes=["pending_edits", "files_read", "last_test_result", "log"],
)
def create_file(state: State, path: str, content: str) -> State:
    """Stage a brand-new file. Refuses if ``path`` already exists in
    the workspace or is already pending an edit, so create_file can't
    silently clobber a file the agent should have read first.

    The new path is implicitly added to ``files_read`` since you just
    wrote it, so a subsequent ``edit_file`` on the same path works
    without an intermediate ``read_file``. Like ``edit_file``, this
    sets ``last_test_result=unknown`` so the agent must re-test
    before commit.

    Args:
        path: New filename. Must not collide with anything in the
            current workspace or pending_edits.
        content: Full contents of the new file.
    """
    workspace = state["workspace"]
    pending = state.get("pending_edits", {})
    if not path.strip():
        raise ValueError("path must not be empty")
    if path in workspace:
        raise ValueError(
            f"file {path!r} already exists in workspace. "
            f"Use read_file then edit_file to modify it instead."
        )
    if path in pending:
        raise ValueError(
            f"file {path!r} is already staged as a pending edit or create. "
            f"Use edit_file to modify the staged version."
        )
    new_pending = {**pending, path: content}
    files_read = state.get("files_read", [])
    if path not in files_read:
        files_read = [*files_read, path]
    log = [
        *state.get("log", []),
        f"Created {path} ({len(content)} bytes). Tests now stale.",
    ]
    return state.update(
        pending_edits=new_pending,
        files_read=files_read,
        last_test_result="unknown",
        log=log,
    )


@action(
    reads=["workspace", "files_read", "pending_edits", "log"],
    writes=["pending_edits", "last_test_result", "log"],
)
def edit_file(state: State, path: str, new_content: str) -> State:
    """Stage an edit to ``path``. Refuses if ``path`` hasn't been
    read yet in this session.

    Args:
        path: File to edit. Must already appear in ``files_read``.
        new_content: Full new contents of the file.
    """
    workspace = state["workspace"]
    pending = state.get("pending_edits", {})
    files_read = state.get("files_read", [])
    if path not in workspace and path not in pending:
        known = sorted(set(workspace) | set(pending))
        raise ValueError(f"no such file {path!r}. Known paths (workspace + pending): {known}.")
    if path not in files_read:
        raise ValueError(f"must read {path!r} before editing it. Files read so far: {files_read}.")
    new_pending = {**pending, path: new_content}
    log = [
        *state.get("log", []),
        f"Staged edit to {path} ({len(new_content)} bytes). Tests now stale.",
    ]
    # Editing invalidates the prior test run.
    return state.update(pending_edits=new_pending, last_test_result="unknown", log=log)


@action(reads=["pending_edits", "log"], writes=["last_test_result", "log"])
def run_tests(state: State, result: str = "passed") -> State:
    """Run the test suite against the current workspace + pending edits.

    Args:
        result: For demo control. ``passed`` (default) or ``failed``.
            In a real version this would actually shell out to pytest.
    """
    if result not in {"passed", "failed"}:
        raise ValueError(f"result must be passed|failed; got {result!r}")
    n_changed = len(state.get("pending_edits", {}))
    log = [
        *state.get("log", []),
        f"Tests run against {n_changed} pending edit(s): {result}.",
    ]
    return state.update(last_test_result=result, log=log)


@action(
    reads=["workspace", "pending_edits", "commits", "log"],
    writes=["workspace", "pending_edits", "last_test_result", "commits", "log"],
)
def commit(state: State, message: str) -> State:
    """Commit pending edits to the workspace. Transition-gated on
    pending_edits being non-empty *and* the most recent test run
    having passed.

    Args:
        message: Commit message. Required.
    """
    if not message.strip():
        raise ValueError("commit message must not be empty")
    pending = state["pending_edits"]
    workspace = {**state["workspace"], **pending}
    new_commit = {"message": message, "files": list(pending.keys())}
    commits = [*state.get("commits", []), new_commit]
    log = [
        *state.get("log", []),
        f"Committed {len(pending)} file(s): {message!r}.",
    ]
    # Fresh commit: nothing pending, test result reset.
    return state.update(
        workspace=workspace,
        pending_edits={},
        last_test_result="unknown",
        commits=commits,
        log=log,
    )


@action(reads=["workspace", "log"], writes=["pending_delete", "log"])
def request_delete(state: State, path: str) -> State:
    """Stage a deletion. Doesn't actually delete; ``confirm_delete``
    has to run after. This two-step makes destructive ops un-fat-finger-able.

    Args:
        path: File to delete. Must exist in the workspace.
    """
    workspace = state["workspace"]
    if path not in workspace:
        raise ValueError(f"no such file {path!r}. Workspace: {sorted(workspace)}")
    log = [*state.get("log", []), f"Delete requested: {path}. Awaiting confirm."]
    return state.update(pending_delete=path, log=log)


@action(
    reads=["workspace", "pending_delete", "files_read", "log"],
    writes=["workspace", "pending_delete", "files_read", "log"],
)
def confirm_delete(state: State) -> State:
    """Actually delete the file staged by ``request_delete``.
    Transition-gated on ``pending_delete`` being set."""
    path = state["pending_delete"]
    workspace = {k: v for k, v in state["workspace"].items() if k != path}
    files_read = [p for p in state.get("files_read", []) if p != path]
    log = [*state.get("log", []), f"Deleted {path}."]
    return state.update(
        workspace=workspace,
        pending_delete=None,
        files_read=files_read,
        log=log,
    )


# ── transitions ─────────────────────────────────────────────────────


# Every non-gated action is reachable from every action. Gated targets
# (commit, confirm_delete) carry conditions. Built programmatically to
# avoid an unreadable 7x7 matrix of literal tuples.
_ALL_ACTIONS = [
    "list_files",
    "read_file",
    "create_file",
    "edit_file",
    "run_tests",
    "commit",
    "request_delete",
    "confirm_delete",
]
_UNCONDITIONAL_TARGETS = [
    "list_files",
    "read_file",
    "create_file",
    "edit_file",
    "run_tests",
    "request_delete",
]
_GATED_TARGETS = [
    (
        "commit",
        Condition.expr("len(pending_edits) > 0 and last_test_result == 'passed'"),
    ),
    ("confirm_delete", Condition.expr("pending_delete is not None")),
]


def _build_transitions() -> list[tuple]:
    # Burr only permits one "default" transition per source. Use a
    # trivially-true Condition so multiple non-gated targets from the
    # same source don't collide.
    always = Condition.expr("True")
    transitions: list[tuple] = []
    for src in _ALL_ACTIONS:
        for tgt in _UNCONDITIONAL_TARGETS:
            transitions.append((src, tgt, always))
        for tgt, cond in _GATED_TARGETS:
            transitions.append((src, tgt, cond))
    return transitions


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            list_files=list_files,
            read_file=read_file,
            create_file=create_file,
            edit_file=edit_file,
            run_tests=run_tests,
            commit=commit,
            request_delete=request_delete,
            confirm_delete=confirm_delete,
        )
        .with_transitions(*_build_transitions())
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            workspace=dict(_INITIAL_WORKSPACE),
            files_read=[],
            pending_edits={},
            pending_delete=None,
            last_test_result="unknown",
            commits=[],
            log=[],
        )
        .with_entrypoint("list_files")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="local-shell",
        instructions=(
            "A tiny local-shell-style FSM. Start every session with "
            "list_files (the entrypoint). Then: read_file(path), "
            "create_file(path, content), edit_file(path, new_content), "
            "run_tests([result]), commit(message), "
            "request_delete(path), confirm_delete. Safety rules the "
            "server enforces: (1) you must read a file before editing "
            "it; (2) create_file refuses if the path already exists "
            "or is staged; (3) you must run_tests with result=passed "
            "before commit, and any edit_file or create_file "
            "invalidates the prior test result; (4) deletion is "
            "two-step (request then confirm). Read burr://state for "
            "workspace, files_read, pending_edits, last_test_result, "
            "commits. Read burr://next to see currently-legal actions."
        ),
    )


if __name__ == "__main__":
    build_server().run()
