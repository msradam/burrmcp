"""burr-git-review: gated wrapper around git CLI subcommands.

Walks one legal sequence::

    status -> recent_commits -> show_commit (1+) -> summarize

``status`` runs ``git status --short --branch``.
``recent_commits`` runs ``git log --oneline -<N>``.
``show_commit`` runs ``git show --stat <sha>`` and validates the SHA
came from the most recent ``recent_commits`` output.
``summarize`` composes a structured report from what's been collected.

Defaults to the current working directory. Override via ``GIT_REPO``::

    GIT_REPO=/path/to/repo uv run python examples/git_review.py
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ServingMode, ValidationFailed, mount

REPO_DIR = Path(os.environ.get("GIT_REPO", os.getcwd())).resolve()


def _git(*args: str) -> tuple[int, str, str]:
    """Run a git command in ``REPO_DIR`` and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(  # explicit git invocation in a scoped repo
            ["git", *args],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


@action(reads=[], writes=["status_output", "branch", "checked_status_at"])
async def status(state: State) -> State:
    """Inspect the working tree and current branch."""
    rc, stdout, stderr = _git("status", "--short", "--branch")
    if rc != 0:
        raise RuntimeError(f"git status failed: {stderr}")
    branch_line = stdout.splitlines()[0] if stdout else ""
    branch = (
        branch_line.replace("## ", "").split("...")[0]
        if branch_line.startswith("## ")
        else "unknown"
    )
    return state.update(
        status_output=stdout,
        branch=branch,
        checked_status_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["branch"], writes=["recent_commits"])
async def recent_commits(state: State, count: int = 10) -> State:
    """List the most recent N commits on the current branch.

    Returns one line per commit: ``<short-sha> <subject>``. The SHAs in
    this output are the only ones ``show_commit`` will accept; the
    validator enforces that.
    """
    rc, stdout, stderr = _git("log", "--oneline", f"-{int(count)}")
    if rc != 0:
        raise RuntimeError(f"git log failed: {stderr}")
    return state.update(recent_commits=stdout)


@action(
    reads=["recent_commits"],
    writes=["commit_details", "inspected_commits", "done_inspecting"],
)
async def show_commit(state: State, sha: str, done: bool = False) -> State:
    """Show the details of one commit.

    Pass ``done=True`` on the last commit you want to look at to move
    on to ``summarize``. ``done=False`` (the default) loops back so
    you can inspect another commit.
    """
    rc, stdout, stderr = _git("show", "--stat", "--no-color", sha)
    if rc != 0:
        raise RuntimeError(f"git show failed: {stderr}")
    details = dict(state.get("commit_details") or {})
    details[sha] = stdout
    inspected = list(state.get("inspected_commits") or [])
    if sha not in inspected:
        inspected.append(sha)
    return state.update(
        commit_details=details,
        inspected_commits=inspected,
        done_inspecting=bool(done),
    )


@action(
    reads=["status_output", "branch", "recent_commits", "inspected_commits"],
    writes=["summary"],
)
async def summarize(state: State) -> State:
    """Produce a deterministic summary of what was inspected.

    No LLM call inside the action; the synthesis is just structured
    Python over the state we collected. The agent can layer a prose
    summary on top by reading this resource and adding context.
    """
    status_output = state.get("status_output", "") or ""
    status_lines = status_output.splitlines()
    # Line 0 is "## <branch>"; later lines are working-tree changes.
    working_tree_clean = len(status_lines) <= 1
    summary = {
        "repo": str(REPO_DIR),
        "branch": state.get("branch"),
        "working_tree_clean": working_tree_clean,
        "uncommitted_changes": status_lines[1:] if not working_tree_clean else [],
        "commits_listed": len((state.get("recent_commits") or "").splitlines()),
        "commits_inspected": list(state.get("inspected_commits") or []),
        "summarised_at": datetime.now(UTC).isoformat(),
    }
    return state.update(summary=summary)


def _validate_show_commit(state: dict, inputs: dict) -> None:
    """Refuse SHAs that didn't appear in the most recent listing.

    Prevents the agent from inventing a SHA or pasting one from an
    earlier session. ``recent_commits`` must have run for this session
    and the requested SHA must appear in its output (matched on the
    short-SHA prefix that ``git log --oneline`` prints).
    """
    sha = inputs.get("sha", "") or ""
    if len(sha) < 4:
        raise ValidationFailed(
            "sha must be at least 4 characters",
            details={"received": sha},
        )
    recent = state.get("recent_commits") or ""
    if not recent:
        raise ValidationFailed(
            "no recent_commits captured yet; run recent_commits first",
        )
    # ``git log --oneline`` lines start with the short SHA; we accept
    # the requested sha as a prefix match to handle both short and
    # full hex inputs.
    listed_shas = [line.split()[0] for line in recent.splitlines() if line.strip()]
    if not any(s.startswith(sha) or sha.startswith(s) for s in listed_shas):
        raise ValidationFailed(
            "sha not in the most recent commits listing; pick from there",
            details={"requested": sha, "listed_shas": listed_shas},
        )
    return


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            status=status,
            recent_commits=recent_commits,
            show_commit=show_commit,
            summarize=summarize,
        )
        .with_transitions(
            ("status", "recent_commits"),
            ("recent_commits", "show_commit"),
            # Self-loop while the agent keeps inspecting more commits.
            ("show_commit", "show_commit", Condition.expr("done_inspecting == False")),
            # When the agent passes done=True, move on to summarise.
            ("show_commit", "summarize", Condition.expr("done_inspecting == True")),
        )
        .with_state(
            status_output=None,
            branch=None,
            checked_status_at=None,
            recent_commits=None,
            commit_details=None,
            inspected_commits=None,
            done_inspecting=False,
            summary=None,
        )
        .with_entrypoint("status")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="burr-git-review",
        instructions=(
            f"Git review FSM operating on {REPO_DIR}. Walk the legal "
            "sequence: status -> recent_commits -> show_commit (one "
            "or more times) -> summarize. show_commit's sha must come "
            "from the recent_commits listing in this session."
        ),
        input_validators={"show_commit": _validate_show_commit},
        action_timeout_seconds=60,
    )


if __name__ == "__main__":
    build_server().run()
