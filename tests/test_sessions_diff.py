"""``theodosia sessions diff <a> <b>``: cross-session post-mortem comparison."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from theodosia.cli import _diff_state_dicts, build_cli

runner = CliRunner()


def test_diff_state_dicts_only_in_left():
    only_l, only_r, changed = _diff_state_dicts({"a": 1, "b": 2}, {"a": 1})
    assert only_l == ["b"]
    assert only_r == []
    assert changed == []


def test_diff_state_dicts_only_in_right():
    only_l, only_r, changed = _diff_state_dicts({"a": 1}, {"a": 1, "c": 3})
    assert only_l == []
    assert only_r == ["c"]
    assert changed == []


def test_diff_state_dicts_changed():
    only_l, only_r, changed = _diff_state_dicts({"a": 1, "b": 2}, {"a": 1, "b": 99})
    assert only_l == []
    assert only_r == []
    assert changed == [("b", 2, 99)]


def test_diff_state_dicts_identical():
    only_l, only_r, changed = _diff_state_dicts({"a": 1}, {"a": 1})
    assert (only_l, only_r, changed) == ([], [], [])


def _seed_session(home, project, app_id, mtime: int, steps: list[dict]) -> None:
    """Write a minimal Burr tracker log for one session."""
    sess_dir = home / project / app_id
    sess_dir.mkdir(parents=True)
    log = sess_dir / "log.jsonl"
    lines = []
    for i, step in enumerate(steps):
        lines.append(
            json.dumps(
                {
                    "type": "begin_entry",
                    "sequence_id": i,
                    "action": step["action"],
                    "start_time": f"2026-05-29T10:00:0{i}",
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "end_entry",
                    "sequence_id": i,
                    "action": step["action"],
                    "end_time": f"2026-05-29T10:00:0{i + 1}",
                    "state": {**step["state"], "__PRIOR_STEP": step["action"]},
                }
            )
        )
    log.write_text("\n".join(lines))
    import os

    os.utime(sess_dir, (mtime, mtime))


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    """Two tracker sessions under the same project; B diverges at step 2."""
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".theodosia"
    _seed_session(
        home,
        "demo",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        mtime=1700000000,
        steps=[
            {"action": "start", "state": {"x": 1}},
            {"action": "pay", "state": {"x": 1, "paid": True}},
        ],
    )
    _seed_session(
        home,
        "demo",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        mtime=1700001000,
        steps=[
            {"action": "start", "state": {"x": 1}},
            {"action": "cancel", "state": {"x": 1, "cancelled": True}},
        ],
    )
    return home


def test_diff_shows_action_path_divergence(two_sessions):
    cli = build_cli()
    result = runner.invoke(
        cli,
        [
            "sessions",
            "diff",
            "aaaaaaaa",
            "bbbbbbbb",
            "--home",
            str(two_sessions),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["a"]["actions"] == ["start", "pay"]
    assert payload["b"]["actions"] == ["start", "cancel"]
    keys_in_b_only = payload["state_diff"]["only_in_b"]
    keys_in_a_only = payload["state_diff"]["only_in_a"]
    assert "paid" in keys_in_a_only
    assert "cancelled" in keys_in_b_only
