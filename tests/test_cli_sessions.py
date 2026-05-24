"""CLI: `sessions` subcommands and rich-rendered observability.

The sessions surface reads Burr's ``LocalTrackingClient`` JSONL logs
out of ``~/.burr`` (or ``--burr-home``). These tests build a synthetic
tracker tree under ``tmp_path`` so they don't depend on any real Burr
session having been recorded on the dev's machine.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from burrmcp.cli import _read_steps, _resolve_app, _state_diff_text, app

runner = CliRunner()


def _write_log(log_path: Path, entries: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _begin(seq: int, action: str, ts: str = "2026-05-24T12:00:00.000000") -> dict:
    return {
        "type": "begin_entry",
        "start_time": ts,
        "action": action,
        "inputs": {},
        "sequence_id": seq,
    }


def _end(
    seq: int,
    action: str,
    state: dict,
    ts: str = "2026-05-24T12:00:00.500000",
    exception: str | None = None,
) -> dict:
    return {
        "type": "end_entry",
        "end_time": ts,
        "action": action,
        "result": None,
        "exception": exception,
        "state": {**state, "__SEQUENCE_ID": seq},
        "sequence_id": seq,
    }


# == _read_steps ======================================================


def test_read_steps_pairs_begin_and_end(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            _begin(0, "start"),
            _end(0, "start", {"stage": "ordered"}),
            _begin(1, "pay"),
            _end(1, "pay", {"stage": "paid"}),
        ],
    )
    rows = _read_steps(log)
    assert [r.seq for r in rows] == [0, 1]
    assert [r.action for r in rows] == ["start", "pay"]
    assert [r.status for r in rows] == ["ok", "ok"]
    assert rows[0].duration_ms == 500.0


def test_read_steps_flags_errors(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            _begin(0, "broken"),
            _end(0, "broken", {}, exception="Traceback (most recent call last):\nValueError: bad"),
        ],
    )
    rows = _read_steps(log)
    assert rows[0].status == "error"
    assert "ValueError: bad" in (rows[0].error_summary or "")


def test_read_steps_marks_unfinished_running(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(log, [_begin(0, "in_flight")])
    rows = _read_steps(log)
    assert rows[0].status == "running"
    assert rows[0].duration_ms is None


# == state diff ======================================================


def test_state_diff_first_step_shows_nonempty():
    out = _state_diff_text({"x": 1, "empty": "", "stage": "ordered"}, prev_state=None)
    assert "x=1" in out
    assert "stage=ordered" in out
    assert "empty" not in out


def test_state_diff_subsequent_shows_only_changes():
    prev = {"x": 1, "stage": "ordered", "qty": 2}
    curr = {"x": 1, "stage": "paid", "qty": 2}
    out = _state_diff_text(curr, prev)
    assert "stage=paid" in out
    assert "x=" not in out  # unchanged values omitted


# == _resolve_app ====================================================


def test_resolve_app_uses_most_recent_when_unspecified(tmp_path):
    proj = tmp_path / "myproj"
    (proj / "app-old").mkdir(parents=True)
    (proj / "app-new").mkdir(parents=True)
    # Touch app-new last so its mtime is newer.
    (proj / "app-new" / "log.jsonl").write_text("{}\n")
    log_path, resolved_proj, resolved_app = _resolve_app(tmp_path, None, None)
    assert resolved_proj == "myproj"
    assert resolved_app == "app-new"
    assert log_path == proj / "app-new" / "log.jsonl"


def test_resolve_app_accepts_prefix(tmp_path):
    proj = tmp_path / "myproj"
    (proj / "abc12345-full-uuid").mkdir(parents=True)
    log_path, _, resolved_app = _resolve_app(tmp_path, "myproj", "abc12345")
    assert resolved_app == "abc12345-full-uuid"
    assert log_path.name == "log.jsonl"


# == sessions sub-app commands =======================================


def test_sessions_help_lists_subcommands():
    result = runner.invoke(app, ["sessions", "--help"])
    assert result.exit_code == 0
    assert "ls" in result.output
    assert "show" in result.output
    assert "tail" in result.output


def test_sessions_ls_renders_table(tmp_path):
    proj = tmp_path / "coffee-order-demo"
    _write_log(
        proj / "abc123" / "log.jsonl",
        [_begin(0, "take_order"), _end(0, "take_order", {"stage": "ordered"})],
    )
    result = runner.invoke(app, ["sessions", "ls", "--burr-home", str(tmp_path)])
    assert result.exit_code == 0
    assert "coffee-order-demo" in result.output
    assert "abc123" in result.output


def test_sessions_ls_json_emits_valid_json(tmp_path):
    proj = tmp_path / "demo"
    _write_log(
        proj / "uuid1" / "log.jsonl",
        [_begin(0, "go"), _end(0, "go", {"x": 1})],
    )
    result = runner.invoke(app, ["sessions", "ls", "--burr-home", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["project"] == "demo"
    assert payload[0]["apps"][0]["app_id"] == "uuid1"
    assert payload[0]["apps"][0]["last_action"] == "go"


def test_sessions_show_renders_steps_table(tmp_path):
    _write_log(
        tmp_path / "demo" / "u1" / "log.jsonl",
        [
            _begin(0, "step_a"),
            _end(0, "step_a", {"k": 1}),
            _begin(1, "step_b"),
            _end(1, "step_b", {"k": 2}),
        ],
    )
    result = runner.invoke(
        app, ["sessions", "show", "u1", "--project", "demo", "--burr-home", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "step_a" in result.output
    assert "step_b" in result.output


def test_sessions_show_json_emits_step_records(tmp_path):
    _write_log(
        tmp_path / "demo" / "u1" / "log.jsonl",
        [_begin(0, "only_one"), _end(0, "only_one", {"k": 1})],
    )
    result = runner.invoke(
        app,
        ["sessions", "show", "u1", "--project", "demo", "--burr-home", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["project"] == "demo"
    assert payload["app_id"] == "u1"
    assert payload["steps"][0]["action"] == "only_one"
    assert payload["steps"][0]["status"] == "ok"


def test_sessions_show_handles_no_steps(tmp_path):
    (tmp_path / "demo" / "u1").mkdir(parents=True)
    (tmp_path / "demo" / "u1" / "log.jsonl").write_text("")
    result = runner.invoke(
        app, ["sessions", "show", "u1", "--project", "demo", "--burr-home", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "No steps" in result.output


def test_watch_alias_lives_at_top_level():
    """`burrmcp watch` should still exist as a top-level command."""
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "Alias" in result.output or "sessions tail" in result.output
