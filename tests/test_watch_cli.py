"""`burrmcp watch` subcommand: terminal observability for Burr trackers.

The CLI tails the LocalTrackingClient JSONL log for a session and pretty-
prints each `end_entry`. Tests exercise the resolution/rendering helpers
directly; the tail loop itself is straightforward polling and is exercised
manually rather than asserted on.
"""

from __future__ import annotations

import json

import pytest

from burrmcp.cli import (
    _render_line,
    _resolve_target,
    _short_timestamp,
    _short_value,
)


def _make_burr_home(tmp_path, layout):
    """Build a tmp ~/.burr/<project>/<app>/log.jsonl tree.

    `layout` maps project name -> list of (app_id, mtime_offset_seconds)
    where mtime_offset_seconds is added to a base time so the test can
    deterministically order directories by mtime.
    """
    import os
    import time as _time

    base = _time.time()
    home = tmp_path / "burr_home"
    home.mkdir()
    for project, apps in layout.items():
        proj_dir = home / project
        proj_dir.mkdir()
        latest = base
        for app_id, offset in apps:
            app_dir = proj_dir / app_id
            app_dir.mkdir()
            log = app_dir / "log.jsonl"
            log.write_text("")
            atime = base + offset
            os.utime(app_dir, (atime, atime))
            latest = max(latest, atime)
        os.utime(proj_dir, (latest, latest))
    return home


def test_resolve_target_picks_most_recent_project_and_app(tmp_path):
    home = _make_burr_home(
        tmp_path,
        {
            "old-project": [("app-old", 0)],
            "new-project": [("app-a", 10), ("app-b", 20)],
        },
    )
    project, app_id = _resolve_target(home, None, None)
    assert project == "new-project"
    assert app_id == "app-b"


def test_resolve_target_honors_explicit_project(tmp_path):
    home = _make_burr_home(
        tmp_path,
        {
            "p1": [("a", 5)],
            "p2": [("x", 10), ("y", 20)],
        },
    )
    project, app_id = _resolve_target(home, "p1", None)
    assert project == "p1"
    assert app_id == "a"


def test_resolve_target_honors_explicit_app(tmp_path):
    home = _make_burr_home(tmp_path, {"p": [("a", 10), ("b", 20)]})
    project, app_id = _resolve_target(home, "p", "a")
    assert project == "p"
    assert app_id == "a"


def test_resolve_target_returns_nones_when_empty(tmp_path):
    home = tmp_path / "empty_burr_home"
    home.mkdir()
    assert _resolve_target(home, None, None) == (None, None)


def test_resolve_target_skips_dotdirs(tmp_path):
    """`.metadata` style dirs shouldn't be picked up as projects."""
    home = _make_burr_home(tmp_path, {"real-project": [("a", 5)]})
    hidden = home / ".cache_metadata"
    hidden.mkdir()
    project, _ = _resolve_target(home, None, None)
    assert project == "real-project"


def test_render_line_emits_end_entry(capsys):
    record = {
        "type": "end_entry",
        "end_time": "2026-05-19T14:22:01.123456",
        "action": "tick",
        "sequence_id": 3,
        "state": {"counter": 2, "log": ["a"], "__internal": "hidden"},
    }
    _render_line(json.dumps(record))
    out = capsys.readouterr().out
    assert "14:22:01" in out
    assert "seq   3" in out
    assert "tick" in out
    assert "counter=2" in out
    # Internal keys are filtered out.
    assert "__internal" not in out


def test_render_line_renders_exception(capsys):
    record = {
        "type": "end_entry",
        "end_time": "2026-05-19T14:22:01",
        "action": "tick",
        "sequence_id": 5,
        "exception": "ValueError: bad input",
        "state": {},
    }
    _render_line(json.dumps(record))
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "ValueError" in out


def test_render_line_skips_begin_entry_and_other_types(capsys):
    for record in [
        {"type": "begin_entry", "action": "tick"},
        {"type": "begin_span", "span_id": "s1"},
        {"type": "attribute", "key": "k", "value": "v"},
    ]:
        _render_line(json.dumps(record))
    assert capsys.readouterr().out == ""


def test_render_line_handles_malformed_json(capsys):
    _render_line("not json at all")
    _render_line("")
    _render_line("{partial json")
    assert capsys.readouterr().out == ""


def test_short_timestamp_extracts_time_from_iso():
    assert _short_timestamp("2026-05-19T14:22:01.123456") == "14:22:01"
    assert _short_timestamp("2026-05-19T14:22:01") == "14:22:01"


def test_short_timestamp_returns_input_when_no_t():
    assert _short_timestamp("just-a-string") == "just-a-string"
    assert _short_timestamp("") == ""


def test_short_value_truncates_long_strings():
    truncated = _short_value("a" * 50, limit=20)
    assert len(truncated) == 20
    assert truncated.startswith("'a")
    assert truncated.endswith("...")
    assert _short_value(123) == "123"
    assert _short_value([1, 2, 3]) == "[1, 2, 3]"
    assert _short_value("short") == "'short'"


@pytest.mark.parametrize("dotdir_name", [".metadata", ".cache", ".tmp"])
def test_resolve_target_skips_named_dotdirs(tmp_path, dotdir_name):
    home = _make_burr_home(tmp_path, {"real-project": [("a", 5)]})
    (home / dotdir_name).mkdir()
    project, _ = _resolve_target(home, None, None)
    assert project == "real-project"
