"""CLI: import-target resolution and Typer command surface.

The CLI's ``serve`` command is hard to test end-to-end without spawning
a subprocess (``server.run`` blocks on stdio). Cover the importable-
target resolver and the Typer command shape here; the integration is
covered by exercising the same code path (``mount``) in other tests.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from theodosia import cli
from theodosia.cli import _import_target, app, build_cli

runner = CliRunner()


@pytest.fixture
def restore_branding():
    """build_cli mutates a module-level branding singleton; restore it so a
    rebranded CLI in one test doesn't leak into tests that use the default app.

    The singleton is now mutated in place (so cross-module imports stay live),
    so save the fields and copy them back, rather than rebinding the name.
    """
    import dataclasses

    saved_fields = dataclasses.asdict(cli._BRANDING)
    yield
    for name, value in saved_fields.items():
        setattr(cli._BRANDING, name, value)


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
    """`theodosia` with no args should show help (no_args_is_help=True)
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
    build_cli("theodosia")  # no baked-in application
    with pytest.raises(SystemExit, match="serve needs a target"):
        cli._resolve_serve_target(None, [])


def test_build_cli_default_home_used_when_flag_absent(restore_branding, tmp_path):
    build_cli("mygraph", home=tmp_path)
    assert cli._resolve_home(None) == tmp_path
    # an explicit flag still wins
    other = tmp_path / "other"
    assert cli._resolve_home(other) == other


def test_build_cli_records_upstream_for_serve(restore_branding):
    """A baked-in upstream map is held on the branding so serve passes it to mount."""
    upstream = {"fs": {"command": "npx", "args": ["-y", "server-filesystem", "."]}}
    build_cli("mygraph", application="coffee_order:build_application", upstream=upstream)
    assert cli._BRANDING.upstream == upstream


# ── render ───────────────────────────────────────────────────────


def test_topology_extracts_entry_terminals_edges():
    topo = cli._topology("coffee_order:build_application", ["examples"], "coffee")
    assert topo.entry == "take_order"
    assert "fulfill" in topo.actions
    assert topo.is_terminal("fulfill")
    assert not topo.is_terminal("take_order")
    assert topo.has_self_loop("add_modifier")


def test_condition_label_suppresses_default():
    class Default:
        name = "default"

    class Real:
        name = "stage == 'paid'"

    assert cli._condition_label(Default()) is None
    assert cli._condition_label(Real()) == "stage == 'paid'"


def test_render_mermaid_emits_statediagram(capsys):
    cli.render("coffee_order:build_application", app_dir=["examples"], mermaid=True)
    out = capsys.readouterr().out
    assert "stateDiagram-v2" in out
    assert "[*] --> take_order" in out
    assert "add_modifier --> add_modifier" in out
    assert "fulfill --> [*]" in out


def test_render_dot_emits_digraph(capsys):
    cli.render("coffee_order:build_application", app_dir=["examples"], dot=True)
    out = capsys.readouterr().out
    assert out.strip().startswith("digraph G {")
    assert '"take_order" -> "pay"' in out


def test_render_rejects_both_mermaid_and_dot():
    with pytest.raises(SystemExit, match="not both"):
        cli.render("coffee_order:build_application", app_dir=["examples"], mermaid=True, dot=True)


def _seed_session(tmp_path, actions_stages):
    """Write a synthetic tracker log under tmp_path and return (home, app_id)."""
    import json

    app_dir = tmp_path / "coffee-order-demo" / "app-1"
    app_dir.mkdir(parents=True)
    rows = []
    for seq, (action, stage) in enumerate(actions_stages):
        rows.append(
            {
                "type": "begin_entry",
                "start_time": "2026-05-25T12:00:00.0",
                "action": action,
                "inputs": {},
                "sequence_id": seq,
            }
        )
        rows.append(
            {
                "type": "end_entry",
                "end_time": "2026-05-25T12:00:00.5",
                "action": action,
                "result": None,
                "exception": None,
                "state": {"stage": stage, "__SEQUENCE_ID": seq},
                "sequence_id": seq,
            }
        )
    (app_dir / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path, "app-1"


def test_session_progress_reports_current_and_visited(tmp_path):
    home, _ = _seed_session(tmp_path, [("take_order", "ordered"), ("pay", "paid")])
    aid, current, visited = cli._session_progress(home, "coffee-order-demo", None)
    assert aid == "app-1"
    assert current == "pay"
    assert visited == {"take_order", "pay"}


def test_render_annotation_highlights_current_node(tmp_path):
    from rich.console import Console

    topo = cli._topology("coffee_order:build_application", ["examples"], "coffee-order")
    group = cli._graph_renderable(
        topo, conditions=False, current="pay", visited={"take_order", "pay"}, session="app-1"
    )
    plain = Console(width=120, no_color=True)
    with plain.capture() as cap:
        plain.print(group)
    out = cap.get()
    assert "at: pay" in out
    assert "● pay" in out  # current node marker
