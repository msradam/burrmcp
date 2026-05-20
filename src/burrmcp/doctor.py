"""Static validation for Burr Applications before mounting them.

``burrmcp doctor module:attr`` runs a set of checks against an
Application (or a factory returning one) and reports findings without
ever calling ``astep``. Catches the classes of bug that only surface at
runtime against the real MCP client:

* import target unresolvable
* factory raises on build
* graph has unreachable actions
* graph has dead-end actions (no outgoing transitions other than the
  terminal node the author intended)
* actions read state keys that nothing writes and nothing seeds in
  ``with_state(...)``
* initial state defines keys no action ever reads (loose noise, not an
  error but worth surfacing)

Importable as a library too: ``from burrmcp.doctor import run_checks``
returns a :class:`DoctorReport` you can fold into your own test suite.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from burr.core import Application

# Keys Burr writes itself. Doctor must ignore these when reasoning
# about user-facing state coverage so it doesn't flag every Application
# as having "unused" sequence-id housekeeping.
_INTERNAL_STATE_KEYS = frozenset({"__SEQUENCE_ID", "__PRIOR_STEP"})


class CheckStatus(str, Enum):  # noqa: UP042  # match adapter.ServingMode convention
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"


@dataclass
class CheckResult:
    """One row in the doctor's findings table.

    ``name`` is a short label; ``message`` is a one-liner; ``details``
    is a list of follow-up lines printed only in verbose mode (or when
    the status is fail/warn).
    """

    name: str
    status: CheckStatus
    message: str = ""
    details: list[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def info(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.INFO)

    @property
    def ok(self) -> bool:
        """True if no failures. Warnings don't block; they're for the
        author's eyes."""
        return self.failed == 0


def run_checks(application: Any) -> DoctorReport:
    """Validate a Burr Application or a callable that returns one.

    A callable is invoked once to obtain the Application. Any
    exception during that call becomes a :class:`CheckStatus.FAIL`
    finding and short-circuits the remaining graph-level checks (we
    can't introspect a graph we couldn't build).
    """
    report = DoctorReport()

    app = _resolve_application(application, report)
    if app is None:
        return report

    report.checks.extend(_check_graph_topology(app))
    report.checks.extend(_check_state_contract(app))
    report.checks.extend(_check_initial_state_usage(app))
    return report


def _resolve_application(application: Any, report: DoctorReport) -> Application | None:
    """Coax ``application`` into a concrete ``burr.core.Application``.

    Accepts: an Application instance, or a callable returning one.
    Anything else fails the "is a Burr Application" check.
    """
    if isinstance(application, Application):
        report.checks.append(
            CheckResult(
                "Resolve target",
                CheckStatus.PASS,
                "got an Application instance",
            )
        )
        return application

    if callable(application):
        try:
            built = application()
        except Exception as exc:
            report.checks.append(
                CheckResult(
                    "Resolve target",
                    CheckStatus.FAIL,
                    f"factory raised {type(exc).__name__}: {exc}",
                )
            )
            return None
        if not isinstance(built, Application):
            report.checks.append(
                CheckResult(
                    "Resolve target",
                    CheckStatus.FAIL,
                    f"factory returned {type(built).__name__}, not a Burr Application",
                )
            )
            return None
        report.checks.append(
            CheckResult(
                "Resolve target",
                CheckStatus.PASS,
                "factory built an Application",
            )
        )
        return built

    report.checks.append(
        CheckResult(
            "Resolve target",
            CheckStatus.FAIL,
            f"target is {type(application).__name__}, not an Application or callable",
        )
    )
    return None


def _check_graph_topology(app: Application) -> list[CheckResult]:
    """Reachability + dead-end detection.

    Two findings:
    * "Graph reachability": fail if any action is unreachable from the
      entrypoint via the declared transitions.
    * "Terminal actions": info-level listing of actions with no
      outgoing transitions. Often intentional (a fulfilled order, a
      closed incident) but worth surfacing so authors notice an
      accidental dead-end.
    """
    actions = list(app.graph.actions)
    transitions = list(app.graph.transitions)
    entrypoint = app.graph.entrypoint.name

    all_names = {a.name for a in actions}
    outgoing: dict[str, list[str]] = {name: [] for name in all_names}
    for t in transitions:
        outgoing.setdefault(t.from_.name, []).append(t.to.name)

    reached: set[str] = set()
    queue: deque[str] = deque([entrypoint])
    while queue:
        cur = queue.popleft()
        if cur in reached:
            continue
        reached.add(cur)
        for nxt in outgoing.get(cur, []):
            if nxt not in reached:
                queue.append(nxt)

    results: list[CheckResult] = []
    unreachable = sorted(all_names - reached)
    if unreachable:
        results.append(
            CheckResult(
                "Graph reachability",
                CheckStatus.FAIL,
                f"{len(unreachable)} unreachable action(s) from entrypoint {entrypoint!r}",
                details=[f"unreachable: {name}" for name in unreachable],
            )
        )
    else:
        results.append(
            CheckResult(
                "Graph reachability",
                CheckStatus.PASS,
                f"all {len(all_names)} action(s) reachable from {entrypoint!r}",
            )
        )

    terminals = sorted(name for name in all_names if not outgoing.get(name))
    if terminals:
        results.append(
            CheckResult(
                "Terminal actions",
                CheckStatus.INFO,
                f"{len(terminals)} action(s) with no outgoing transitions",
                details=[f"terminal: {name}" for name in terminals],
            )
        )
    return results


def _check_state_contract(app: Application) -> list[CheckResult]:
    """Every key an action reads must be writable by some action or
    seeded in ``with_state(...)``.

    Otherwise the first read yields ``None`` (or KeyError, depending on
    how the action is written), which is almost always a bug. Reported
    as a warning rather than a failure because authors occasionally
    rely on ``state.get(k, default)`` and reading-then-defaulting is
    legitimate.
    """
    initial_keys = set(app.state.get_all().keys()) - _INTERNAL_STATE_KEYS
    writable: set[str] = set(initial_keys)
    for a in app.graph.actions:
        writable.update(a.writes or [])

    problems: list[str] = []
    for a in app.graph.actions:
        missing = [r for r in (a.reads or []) if r not in writable]
        if missing:
            problems.append(f"{a.name} reads {missing} (nothing writes or seeds them)")

    if problems:
        return [
            CheckResult(
                "State contract",
                CheckStatus.WARN,
                f"{len(problems)} action(s) read state keys with no writer or seed",
                details=problems,
            )
        ]
    return [
        CheckResult(
            "State contract",
            CheckStatus.PASS,
            "every action's reads are covered by writes or initial state",
        )
    ]


def _check_initial_state_usage(app: Application) -> list[CheckResult]:
    """Flag initial-state keys nothing reads AND nothing writes.

    A key that's seeded then written-but-never-read is part of the
    public state contract the MCP client sees via ``burr://state``,
    not orphan. Only keys that nobody reads *and* nobody writes are
    dead weight worth surfacing.
    """
    initial_keys = set(app.state.get_all().keys()) - _INTERNAL_STATE_KEYS
    read_keys: set[str] = set()
    write_keys: set[str] = set()
    for a in app.graph.actions:
        read_keys.update(a.reads or [])
        write_keys.update(a.writes or [])

    orphan = sorted(initial_keys - read_keys - write_keys)
    if orphan:
        return [
            CheckResult(
                "Initial state usage",
                CheckStatus.INFO,
                f"{len(orphan)} initial-state key(s) no action reads or writes",
                details=[f"orphan: {k}" for k in orphan],
            )
        ]
    return [
        CheckResult(
            "Initial state usage",
            CheckStatus.PASS,
            "every initial-state key is read or written by at least one action",
        )
    ]


# ── formatting ──────────────────────────────────────────────────────


_STATUS_TAG = {
    CheckStatus.PASS: "[PASS]",
    CheckStatus.FAIL: "[FAIL]",
    CheckStatus.WARN: "[WARN]",
    CheckStatus.INFO: "[INFO]",
}


def format_report(report: DoctorReport, verbose: bool = False) -> str:
    """Render a :class:`DoctorReport` for the CLI.

    ``verbose=True`` prints every check's message and details. Default
    keeps PASS rows to a single line and only expands FAIL/WARN/INFO
    details, the same density a developer expects from ``ruff`` or
    ``mypy``.
    """
    lines: list[str] = []
    for c in report.checks:
        tag = _STATUS_TAG[c.status]
        head = f"{tag} {c.name}"
        if c.message:
            head += f": {c.message}"
        lines.append(head)
        show_details = verbose or c.status in (
            CheckStatus.FAIL,
            CheckStatus.WARN,
            CheckStatus.INFO,
        )
        if show_details and c.details:
            lines.extend(f"       {d}" for d in c.details)

    parts: list[str] = []
    if report.passed:
        parts.append(f"{report.passed} passed")
    if report.failed:
        parts.append(f"{report.failed} failed")
    if report.warnings:
        parts.append(f"{report.warnings} warnings")
    if report.info:
        parts.append(f"{report.info} info")
    lines.append("")
    lines.append("Doctor: " + (", ".join(parts) if parts else "no checks ran"))
    return "\n".join(lines)


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "format_report",
    "run_checks",
]
