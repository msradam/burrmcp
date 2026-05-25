"""CLI: import-target resolution and Typer command surface.

The CLI's ``serve`` command is hard to test end-to-end without spawning
a subprocess (``server.run`` blocks on stdio). Cover the importable-
target resolver and the Typer command shape here; the integration is
covered by exercising the same code path (``mount``) in other tests.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from burrmcp import cli
from burrmcp.cli import _import_target, app, build_cli

runner = CliRunner()


@pytest.fixture
def restore_branding():
    """build_cli mutates a module-level branding singleton; restore it so a
    rebranded CLI in one test doesn't leak into tests that use the default app."""
    saved = cli._BRANDING
    yield
    cli._BRANDING = saved


# ── import-target resolver ────────────────────────────────────────


def test_import_target_resolves_module_and_attr():
    target = _import_target("coffee_order:build_application")
    assert callable(target)


def test_import_target_resolves_application_instance():
    target = _import_target("coffee_order:build_server")
    assert callable(target)


def test_import_target_rejects_missing_colon():
    with pytest.raises(SystemExit, match="must be of the form"):
        _import_target("coffee_order")


def test_import_target_rejects_bad_module():
    with pytest.raises(SystemExit, match="cannot import module"):
        _import_target("no_such_module_at_all:thing")


def test_import_target_rejects_missing_attr():
    with pytest.raises(SystemExit, match="has no attribute"):
        _import_target("coffee_order:does_not_exist")


# ── Typer command surface ────────────────────────────────────────


def test_root_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "doctor" in result.output


def test_no_args_prints_help():
    """`burrmcp` with no args should show help (no_args_is_help=True)
    rather than dropping the user into an unhelpful error."""
    result = runner.invoke(app, [])
    # Typer convention: no-args + no_args_is_help -> exit 2 with help printed.
    assert "serve" in result.output
    assert "doctor" in result.output


def test_serve_help_mentions_mode_default():
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--mode" in result.output
    # step is the default ServingMode; render varies by Typer version so
    # just check the mode name appears.
    assert "step" in result.output.lower()


def test_serve_rejects_unknown_mode():
    result = runner.invoke(app, ["serve", "x:y", "--mode", "bogus"])
    assert result.exit_code != 0


def test_doctor_help_mentions_verbose_flag():
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--verbose" in result.output


# ── main() entry point (still callable with argv for tests) ──────


def test_main_returns_zero_on_doctor_clean_target():
    code = cli.main(["doctor", "coffee_order:build_application"])
    assert code == 0


def test_main_returns_nonzero_on_doctor_failure():
    """A target whose factory raises should make doctor exit nonzero."""
    import sys
    import types

    mod = types.ModuleType("cli_test_broken")

    def broken_factory():
        raise RuntimeError("intentional")

    mod.broken_factory = broken_factory
    sys.modules["cli_test_broken"] = mod
    try:
        code = cli.main(["doctor", "cli_test_broken:broken_factory"])
        assert code == 1
    finally:
        del sys.modules["cli_test_broken"]


# ── build_cli: rebranding + baked-in graph ───────────────────────


def test_build_cli_rebrands_prog_name_and_help(restore_branding):
    rebranded = build_cli("mygraph", help="My graph as an MCP server.")
    result = runner.invoke(rebranded, ["--help"])
    assert result.exit_code == 0
    assert "My graph as an MCP server." in result.output
    # serve/doctor/sessions still present under the new brand
    assert "serve" in result.output
    assert "sessions" in result.output


def test_build_cli_bakes_in_application_so_serve_needs_no_target(restore_branding):
    """With a baked-in graph, _resolve_serve_target returns it when target is None."""
    from coffee_order import build_application

    build_cli("mygraph", application=build_application, server_name="mygraph")
    app_or_factory, default_name = cli._resolve_serve_target(None, [])
    assert app_or_factory is build_application
    assert default_name == "mygraph"


def test_build_cli_target_overrides_baked_in(restore_branding):
    build_cli("mygraph", application="coffee_order:build_application")
    app_or_factory, default_name = cli._resolve_serve_target("triage:build_application", [])
    assert callable(app_or_factory)
    assert default_name == "triage"


def test_serve_without_target_or_baked_in_raises(restore_branding):
    build_cli("burrmcp")  # no baked-in application
    with pytest.raises(SystemExit, match="serve needs a target"):
        cli._resolve_serve_target(None, [])


def test_build_cli_default_burr_home_used_when_flag_absent(restore_branding, tmp_path):
    build_cli("mygraph", burr_home=tmp_path)
    assert cli._resolve_home(None) == tmp_path
    # an explicit flag still wins
    other = tmp_path / "other"
    assert cli._resolve_home(other) == other
