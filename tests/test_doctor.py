"""``burrmcp doctor`` static validation.

Covers the library entrypoint :func:`burrmcp.doctor.run_checks`, the
CLI subcommand, and the individual check functions for unreachable
actions, dead-end terminals, undefined state reads, and unused initial
state seeds.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from burr.core import ApplicationBuilder, State, action

from burrmcp import cli
from burrmcp.doctor import (
    CheckStatus,
    DoctorReport,
    format_report,
    run_checks,
)

# ── helpers ──────────────────────────────────────────────────────────


@action(reads=[], writes=["stage"])
def begin(state: State) -> State:
    return state.update(stage="begun")


@action(reads=["stage"], writes=["stage"])
def finish(state: State) -> State:
    return state.update(stage="done")


def _two_node_factory():
    return (
        ApplicationBuilder()
        .with_actions(begin=begin, finish=finish)
        .with_transitions(("begin", "finish"))
        .with_state(stage="new")
        .with_entrypoint("begin")
        .build()
    )


def _find(report: DoctorReport, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"no check named {name!r} in report: {[c.name for c in report.checks]}"
    return matches[0]


# ── clean path ──────────────────────────────────────────────────────


def test_clean_factory_passes_all_checks():
    report = run_checks(_two_node_factory)
    assert report.ok
    assert report.failed == 0
    assert _find(report, "Resolve target").status == CheckStatus.PASS
    assert _find(report, "Graph reachability").status == CheckStatus.PASS
    assert _find(report, "State contract").status == CheckStatus.PASS


def test_clean_application_instance_passes():
    report = run_checks(_two_node_factory())
    assert report.ok
    assert _find(report, "Resolve target").message == "got an Application instance"


def test_terminal_actions_surface_as_info():
    """``finish`` has no outgoing transition. That's intentional but
    doctor surfaces it so the author can confirm."""
    report = run_checks(_two_node_factory)
    term = _find(report, "Terminal actions")
    assert term.status == CheckStatus.INFO
    assert any("finish" in d for d in term.details)


# ── resolution failures ────────────────────────────────────────────


def test_factory_raising_fails_resolution():
    def boom():
        raise RuntimeError("nope")

    report = run_checks(boom)
    res = _find(report, "Resolve target")
    assert res.status == CheckStatus.FAIL
    assert "RuntimeError" in res.message
    # Doctor short-circuits: no graph checks ran since there's no app.
    assert [c.name for c in report.checks] == ["Resolve target"]


def test_factory_returning_wrong_type_fails():
    def builder():
        return "not an application"

    report = run_checks(builder)
    res = _find(report, "Resolve target")
    assert res.status == CheckStatus.FAIL
    assert "not a Burr Application" in res.message


def test_non_callable_non_application_fails():
    report = run_checks(42)
    res = _find(report, "Resolve target")
    assert res.status == CheckStatus.FAIL


# ── reachability ────────────────────────────────────────────────────


def test_unreachable_action_flagged_as_failure():
    @action(reads=[], writes=["x"])
    def a(state: State) -> State:
        return state.update(x=1)

    @action(reads=[], writes=["y"])
    def b(state: State) -> State:
        return state.update(y=1)

    @action(reads=[], writes=["z"])
    def orphan(state: State) -> State:
        return state.update(z=1)

    app = (
        ApplicationBuilder()
        .with_actions(a=a, b=b, orphan=orphan)
        .with_transitions(("a", "b"))
        .with_state()
        .with_entrypoint("a")
        .build()
    )
    report = run_checks(app)
    reach = _find(report, "Graph reachability")
    assert reach.status == CheckStatus.FAIL
    assert any("orphan" in d for d in reach.details)
    assert not report.ok


# ── state contract ──────────────────────────────────────────────────


def test_action_reading_undefined_key_warns():
    @action(reads=["phantom"], writes=["x"])
    def reads_phantom(state: State) -> State:
        return state.update(x=state.get("phantom", 0) + 1)

    app = (
        ApplicationBuilder()
        .with_actions(reads_phantom=reads_phantom)
        .with_transitions(("reads_phantom", "reads_phantom"))
        .with_state()
        .with_entrypoint("reads_phantom")
        .build()
    )
    report = run_checks(app)
    contract = _find(report, "State contract")
    assert contract.status == CheckStatus.WARN
    assert any("phantom" in d for d in contract.details)
    # Warnings don't fail the overall report.
    assert report.ok


def test_seed_satisfies_state_contract():
    """A key with no writer but a value in ``with_state`` is fine.
    Common when a config struct is supplied at build time."""

    @action(reads=["config"], writes=["seen"])
    def reads_config(state: State) -> State:
        return state.update(seen=state.get("config"))

    app = (
        ApplicationBuilder()
        .with_actions(reads_config=reads_config)
        .with_transitions(("reads_config", "reads_config"))
        .with_state(config={"k": "v"})
        .with_entrypoint("reads_config")
        .build()
    )
    report = run_checks(app)
    assert _find(report, "State contract").status == CheckStatus.PASS


# ── initial state hygiene ──────────────────────────────────────────


def test_orphan_initial_state_key_reported_as_info():
    """A key that nothing reads AND nothing writes is dead weight."""

    @action(reads=[], writes=["x"])
    def tick(state: State) -> State:
        return state.update(x=1)

    app = (
        ApplicationBuilder()
        .with_actions(tick=tick)
        .with_transitions(("tick", "tick"))
        .with_state(x=0, dangling="never touched")
        .with_entrypoint("tick")
        .build()
    )
    report = run_checks(app)
    usage = _find(report, "Initial state usage")
    assert usage.status == CheckStatus.INFO
    assert any("dangling" in d for d in usage.details)


def test_seeded_write_only_key_is_not_flagged():
    """Seeded keys that get written by an action (but never read by
    another action) are part of the public state contract surfaced
    through ``burr://state``, not orphan."""

    @action(reads=[], writes=["status"])
    def set_status(state: State) -> State:
        return state.update(status="active")

    app = (
        ApplicationBuilder()
        .with_actions(set_status=set_status)
        .with_transitions(("set_status", "set_status"))
        .with_state(status="")
        .with_entrypoint("set_status")
        .build()
    )
    report = run_checks(app)
    assert _find(report, "Initial state usage").status == CheckStatus.PASS


# ── output formatting ──────────────────────────────────────────────


def test_format_report_groups_summary_line():
    report = run_checks(_two_node_factory)
    text = format_report(report)
    assert "Doctor:" in text
    assert "passed" in text


def test_format_report_verbose_expands_pass_details():
    report = run_checks(_two_node_factory)
    text = format_report(report, verbose=True)
    # Without verbose, PASS rows still print their message inline. With
    # verbose, this is the same: what verbose adds is detail-printing
    # for PASS rows, which our current PASS rows don't carry. The
    # invariant we assert is that verbose never omits something the
    # non-verbose form prints.
    text_short = format_report(report, verbose=False)
    for line in text_short.splitlines():
        assert line in text


def test_format_report_failure_includes_details():
    @action(reads=[], writes=["x"])
    def a(state: State) -> State:
        return state.update(x=1)

    @action(reads=[], writes=["y"])
    def orphan(state: State) -> State:
        return state.update(y=1)

    app = (
        ApplicationBuilder()
        .with_actions(a=a, orphan=orphan)
        .with_transitions(("a", "a"))
        .with_state()
        .with_entrypoint("a")
        .build()
    )
    report = run_checks(app)
    text = format_report(report)
    assert "[FAIL]" in text
    assert "orphan" in text


# ── CLI integration ────────────────────────────────────────────────


def test_cli_doctor_clean_target_exits_zero():
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["doctor", "coffee_order:build_application"])
    out = buf.getvalue()
    assert code == 0
    assert "[PASS] Resolve target" in out
    assert "Doctor:" in out


def test_cli_doctor_factory_raise_exits_one():
    """A factory that raises produces a failure exit code so CI can
    catch broken FSMs without running them."""
    # Build a tiny throwaway module in-memory by registering it on
    # sys.modules so _import_target can find it.
    import sys
    import types

    mod = types.ModuleType("doctor_test_broken")

    def broken_factory():
        raise RuntimeError("intentional failure for doctor test")

    mod.broken_factory = broken_factory
    sys.modules["doctor_test_broken"] = mod
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["doctor", "doctor_test_broken:broken_factory"])
        out = buf.getvalue()
        assert code == 1
        assert "[FAIL] Resolve target" in out
        assert "intentional failure" in out
    finally:
        del sys.modules["doctor_test_broken"]


def test_cli_doctor_rejects_bad_target():
    """An unimportable target returns nonzero with the error on stderr."""
    import io
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stderr(buf):
        code = cli.main(["doctor", "no_such_module:anything"])
    assert code == 1
    assert "cannot import module" in buf.getvalue()


def test_cli_doctor_verbose_flag_surfaces_in_help():
    """The Typer command surface advertises --verbose."""
    from typer.testing import CliRunner

    from burrmcp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--verbose" in result.output


def test_cli_doctor_app_dir_surfaces_in_help():
    """The Typer command surface advertises --app-dir."""
    from typer.testing import CliRunner

    from burrmcp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--app-dir" in result.output
