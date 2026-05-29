"""Tests for ``theodosia report`` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from theodosia.cli import build_cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_session(
    root: Path,
    project: str = "demo",
    app_id: str = "abc-123",
    steps: list[tuple[str, str]] | None = None,
    refusals: list[tuple[str, str]] | None = None,
) -> Path:
    """Drop a synthetic Burr tracker session under ``root``."""
    proj_dir = root / project / app_id
    proj_dir.mkdir(parents=True)
    log = proj_dir / "log.jsonl"
    lines: list[str] = []
    for i, (action, status) in enumerate(steps or []):
        lines.append(
            json.dumps(
                {
                    "type": "begin_entry",
                    "action": action,
                    "sequence_id": i,
                    "exception": None,
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "end_entry",
                    "action": action,
                    "sequence_id": i,
                    "exception": None if status == "ok" else "boom",
                }
            )
        )
    log.write_text("\n".join(lines) + ("\n" if lines else ""))
    if refusals:
        sidecar = proj_dir / "refusals.jsonl"
        sidecar.write_text(
            "\n".join(
                json.dumps(
                    {
                        "seq": i,
                        "action": action,
                        "ts": "2026-05-29T00:00:00",
                        "refusal_reason": reason,
                    }
                )
                for i, (action, reason) in enumerate(refusals)
            )
            + "\n"
        )
    return log


def test_report_with_no_session_explodes_helpfully(runner: CliRunner, tmp_path: Path) -> None:
    """Asking for a report on an empty tracker should not crash with a stack trace."""
    cli = build_cli()
    result = runner.invoke(cli, ["report", "--home", str(tmp_path)])
    # _resolve_app raises typer.Exit on no projects; exit code reflects that.
    assert result.exit_code != 0


def test_report_renders_markdown_for_recorded_session(runner: CliRunner, tmp_path: Path) -> None:
    _seed_session(tmp_path, steps=[("query_grafana", "ok"), ("correlate", "ok")])
    cli = build_cli()
    result = runner.invoke(cli, ["report", "abc-123", "--home", str(tmp_path)])
    assert result.exit_code == 0
    out = result.stdout
    assert "# Session report" in out
    assert "abc-123" in out
    assert "## Timeline" in out
    assert "query_grafana" in out
    assert "correlate" in out


def test_report_includes_refusals_section_when_present(runner: CliRunner, tmp_path: Path) -> None:
    _seed_session(
        tmp_path,
        steps=[("ok_action", "ok")],
        refusals=[("forbidden_action", "invalid_transition")],
    )
    cli = build_cli()
    result = runner.invoke(cli, ["report", "abc-123", "--home", str(tmp_path)])
    assert result.exit_code == 0
    assert "## Refusals" in result.stdout
    assert "forbidden_action" in result.stdout
    assert "invalid_transition" in result.stdout


def test_report_no_refusals_section_when_empty(runner: CliRunner, tmp_path: Path) -> None:
    _seed_session(tmp_path, steps=[("ok_action", "ok")])
    cli = build_cli()
    result = runner.invoke(cli, ["report", "abc-123", "--home", str(tmp_path)])
    assert result.exit_code == 0
    assert "## Refusals" not in result.stdout


def test_report_writes_to_out_file(runner: CliRunner, tmp_path: Path) -> None:
    _seed_session(tmp_path, steps=[("a", "ok")])
    out = tmp_path / "report.md"
    cli = build_cli()
    result = runner.invoke(cli, ["report", "abc-123", "--home", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    content = out.read_text()
    assert "# Session report" in content


def test_report_empty_session_renders_zero_steps(runner: CliRunner, tmp_path: Path) -> None:
    """A project/app dir exists but the log is empty: report does not crash."""
    _seed_session(tmp_path, steps=[])
    cli = build_cli()
    result = runner.invoke(cli, ["report", "abc-123", "--home", str(tmp_path)])
    assert result.exit_code == 0
    assert "Steps recorded: 0" in result.stdout


def test_report_webhook_post_is_attempted(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook path should POST application/markdown to the configured URL.

    We monkeypatch urlopen so the test doesn't need a real network endpoint.
    """
    _seed_session(tmp_path, steps=[("a", "ok")])
    captured: dict[str, object] = {}

    class _FakeResp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, *_, **__):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode("utf-8") if req.data else ""
        captured["content_type"] = req.headers.get("Content-type")
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    cli = build_cli()
    result = runner.invoke(
        cli,
        [
            "report",
            "abc-123",
            "--home",
            str(tmp_path),
            "--webhook",
            "https://hooks.example.com/incident",
        ],
    )
    assert result.exit_code == 0
    assert captured["url"] == "https://hooks.example.com/incident"
    assert captured["method"] == "POST"
    assert "Session report" in captured["body"]
    assert captured["content_type"] is not None
    assert "markdown" in captured["content_type"]
