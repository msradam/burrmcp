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


def run_checks(application: Any, *, runtime: bool = False) -> DoctorReport:
    """Validate a Burr Application or a callable that returns one.

    A callable is invoked once to obtain the Application. Any
    exception during that call becomes a :class:`CheckStatus.FAIL`
    finding and short-circuits the remaining graph-level checks (we
    can't introspect a graph we couldn't build).

    With ``runtime=True``, also spin up a real ``mount()`` server and
    probe its wire shape: tool listing, resource listing, ``step``
    output shape (headline at content[0], ``structured_content``
    populated). Useful for confirming the polish surfaces are wired
    end-to-end without a separate client.
    """
    report = DoctorReport()

    app = _resolve_application(application, report)
    if app is None:
        return report

    report.checks.extend(_check_graph_topology(app))
    report.checks.extend(_check_state_contract(app))
    report.checks.extend(_check_initial_state_usage(app))

    if runtime:
        report.checks.extend(_check_runtime(application, app))
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
    writable: set[str] = initial_keys.copy()
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


def _check_runtime(application: Any, app: Application) -> list[CheckResult]:
    """Mount the server in-process and probe its wire shape.

    Verifies the four-tool surface, the ResourcesAsTools transform tools,
    the resource catalog, and the ``step`` result shape (headline at
    content[0], JSON at content[1], ``structured_content`` populated).
    The step probe calls the entrypoint action with empty inputs and
    accepts any outcome (success or refusal); the test is the wire
    shape, not the FSM semantics.
    """
    import asyncio

    async def _probe() -> list[CheckResult]:
        from fastmcp import Client

        from burrmcp.adapter import ServingMode, mount

        results: list[CheckResult] = []
        try:
            server = mount(application, mode=ServingMode.STEP, name="doctor-probe")
        except Exception as exc:
            return [
                CheckResult(
                    "Runtime: mount",
                    CheckStatus.FAIL,
                    f"mount() raised {type(exc).__name__}: {exc}",
                )
            ]
        results.append(CheckResult("Runtime: mount", CheckStatus.PASS, "server built"))

        async with Client(server) as client:
            tools = {t.name for t in await client.list_tools()}
            expected_native = {"step", "reset_session", "fork_at"}
            missing = expected_native - tools
            if missing:
                results.append(
                    CheckResult(
                        "Runtime: native tools",
                        CheckStatus.FAIL,
                        f"missing tools: {sorted(missing)}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "Runtime: native tools",
                        CheckStatus.PASS,
                        f"step + reset_session + fork_at present ({len(tools)} total)",
                    )
                )

            ras_missing = {"list_resources", "read_resource"} - tools
            if ras_missing:
                results.append(
                    CheckResult(
                        "Runtime: ResourcesAsTools",
                        CheckStatus.FAIL,
                        f"transform tools missing: {sorted(ras_missing)}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "Runtime: ResourcesAsTools",
                        CheckStatus.PASS,
                        "list_resources + read_resource exposed",
                    )
                )

            # fork_from_past visibility is conditional; report which path applies.
            try:
                from burr.tracking.client import LocalTrackingClient

                has_tracker = isinstance(getattr(app, "_tracker", None), LocalTrackingClient)
            except ImportError:
                has_tracker = False
            ffp_visible = "fork_from_past" in tools
            if has_tracker and ffp_visible:
                results.append(
                    CheckResult(
                        "Runtime: fork_from_past visibility",
                        CheckStatus.PASS,
                        "tracker attached and fork_from_past visible",
                    )
                )
            elif not has_tracker and not ffp_visible:
                results.append(
                    CheckResult(
                        "Runtime: fork_from_past visibility",
                        CheckStatus.PASS,
                        "no tracker; fork_from_past hidden by Visibility transform",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "Runtime: fork_from_past visibility",
                        CheckStatus.WARN,
                        (f"visibility mismatch (has_tracker={has_tracker}, visible={ffp_visible})"),
                    )
                )

            resources = await client.list_resources()
            resource_uris = {str(r.uri) for r in resources}
            expected_resources = {
                "burr://graph",
                "burr://state",
                "burr://next",
                "burr://history",
                "burr://trace",
                "burr://session",
                "burr://subruns",
            }
            r_missing = expected_resources - resource_uris
            if r_missing:
                results.append(
                    CheckResult(
                        "Runtime: resources",
                        CheckStatus.FAIL,
                        f"missing resources: {sorted(r_missing)}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "Runtime: resources",
                        CheckStatus.PASS,
                        f"{len(expected_resources)} burr:// resources registered",
                    )
                )

            # Probe step with a deliberately unknown action. We're
            # checking the wire shape (headline + json + structured),
            # not the FSM body. Unknown_action gives a clean refusal
            # without exercising Burr's action runner.
            r = await client.call_tool(
                "step",
                {"action": "__doctor_probe__", "inputs": {}},
            )
            problems: list[str] = []
            if len(r.content) < 2:
                problems.append(
                    f"expected >=2 content blocks (headline + json), got {len(r.content)}"
                )
            elif not r.content[0].text.startswith("Step "):
                problems.append(
                    f"content[0] doesn't look like a headline: {r.content[0].text[:60]!r}"
                )
            if r.structured_content is None:
                problems.append("structured_content is None (expected populated dict)")
            elif not isinstance(r.structured_content, dict):
                problems.append(
                    f"structured_content has wrong shape: {type(r.structured_content).__name__}"
                )
            if problems:
                results.append(
                    CheckResult(
                        "Runtime: step result shape",
                        CheckStatus.FAIL,
                        f"{len(problems)} shape issue(s)",
                        details=problems,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "Runtime: step result shape",
                        CheckStatus.PASS,
                        "content[0]=headline, content[1]=json, structured_content=dict",
                    )
                )

        return results

    try:
        return asyncio.run(_probe())
    except Exception as exc:
        return [
            CheckResult(
                "Runtime: probe",
                CheckStatus.FAIL,
                f"probe raised {type(exc).__name__}: {exc}",
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
    lines.extend(("", "Doctor: " + (", ".join(parts) if parts else "no checks ran")))
    return "\n".join(lines)


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "format_report",
    "run_checks",
]
