"""Tests for ``theodosia status`` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from theodosia.cli import build_cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_status_missing_home_succeeds_with_warning(runner: CliRunner, tmp_path: Path) -> None:
    """A nonexistent storage dir is a normal "fresh install" state, not an error."""
    cli = build_cli()
    result = runner.invoke(cli, ["status", "--home", str(tmp_path / "nope")])
    assert result.exit_code == 0
    assert "theodosia" in result.stdout
    assert "missing" in result.stdout.lower() or "(missing)" in result.stdout


def test_status_empty_home_reports_no_projects(runner: CliRunner, tmp_path: Path) -> None:
    cli = build_cli()
    result = runner.invoke(cli, ["status", "--home", str(tmp_path)])
    assert result.exit_code == 0
    assert "no projects" in result.stdout.lower()


def test_status_json_with_empty_home(runner: CliRunner, tmp_path: Path) -> None:
    cli = build_cli()
    result = runner.invoke(cli, ["status", "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["storage_exists"] is True
    assert payload["projects"] == []
    assert "theodosia_version" in payload


def test_status_json_picks_up_a_session(runner: CliRunner, tmp_path: Path) -> None:
    """A project with one app dir and a log.jsonl shows up in the status output."""
    proj = tmp_path / "demo-project"
    app_dir = proj / "abc-app-uuid"
    app_dir.mkdir(parents=True)
    log = app_dir / "log.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "begin_entry",
                "action": "first_action",
                "sequence_id": 0,
                "exception": None,
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "end_entry",
                "action": "first_action",
                "sequence_id": 0,
                "exception": None,
            }
        )
        + "\n"
    )
    cli = build_cli()
    result = runner.invoke(cli, ["status", "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    projects = {p["name"]: p for p in payload["projects"]}
    assert "demo-project" in projects
    proj_info = projects["demo-project"]
    assert proj_info["sessions"] == 1
    assert proj_info["latest"]["app_id"] == "abc-app-uuid"
    assert proj_info["latest"]["last_action"] == "first_action"


def test_status_human_output_renders_table_when_projects_exist(
    runner: CliRunner, tmp_path: Path
) -> None:
    proj = tmp_path / "demo"
    app_dir = proj / "a"
    app_dir.mkdir(parents=True)
    (app_dir / "log.jsonl").write_text(
        json.dumps({"type": "begin_entry", "action": "go", "sequence_id": 0, "exception": None})
        + "\n"
        + json.dumps({"type": "end_entry", "action": "go", "sequence_id": 0, "exception": None})
        + "\n"
    )
    cli = build_cli()
    result = runner.invoke(cli, ["status", "--home", str(tmp_path)])
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "go" in result.stdout  # last action
