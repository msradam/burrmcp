"""CLI: import-target resolution and error messages.

The CLI's serve command is hard to test end-to-end without spawning a
subprocess (server.run blocks on stdio). Cover the importable-target
resolution and argument parsing here; the integration is covered by
exercising the same code path the CLI uses (``mount``) in other tests.
"""

from __future__ import annotations

import pytest

from burr_mcp.cli import _build_parser, _import_target


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


def test_parser_defaults_serve_to_step_mode():
    parser = _build_parser()
    args = parser.parse_args(["serve", "coffee_order:build_application"])
    assert args.command == "serve"
    assert args.mode == "step"
    assert args.target == "coffee_order:build_application"
    assert args.name is None


def test_parser_accepts_mode_and_name_overrides():
    parser = _build_parser()
    args = parser.parse_args(["serve", "x:y", "--mode", "dynamic", "--name", "my-server"])
    assert args.mode == "dynamic"
    assert args.name == "my-server"


def test_parser_rejects_unknown_mode():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "x:y", "--mode", "bogus"])
