"""Unix-health FSM: shellout-based system health triage.

Tests monkey-patch ``_run_check_command`` so the FSM exercises the
parsers against canned tool output and the host's actual state
doesn't leak in. Covers each parser, the configure-input validation,
the full walk through produce_report on a clean host, the full walk
through deep_dive + raise_alert on a critical host, and the
transition gates on triage.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import unix_health
from unix_health import (
    _max_severity,
    _parse_df_all,
    _parse_df_root,
    _parse_free,
    _parse_ps_cpu,
    _parse_ps_rss,
    _parse_ps_summary,
    _parse_ps_zombies,
    _parse_uptime,
    _parse_vm_stat,
    _severity_for,
    _severity_for_load,
    _severity_for_zombies,
    build_server,
)


def _patch_runner(monkeypatch, responses):
    """Replace ``_run_check_command`` with a dict-keyed responder.

    Keys are tuples of args (e.g. ``("df", "-k", "/")``); values are
    the canned stdout. Unknown args raise AssertionError so a missing
    fixture is obvious.
    """
    queues = {k: deque([v]) for k, v in responses.items()}

    async def fake(args, timeout=10.0):
        key = tuple(args)
        if key not in queues:
            raise AssertionError(f"no canned response for {key}; have {list(queues)}")
        q = queues[key]
        if not q:
            raise AssertionError(f"ran out of canned responses for {key}")
        return q[0] if len(q) == 1 else q.popleft()

    monkeypatch.setattr(unix_health, "_run_check_command", fake)


# Canned tool outputs reused across tests ─────────────────────────


_UPTIME_HEALTHY = "12:34:56 up 5 days,  2:13,  3 users,  load average: 0.42, 0.51, 0.61\n"
_DF_HEALTHY = (
    "Filesystem     1024-blocks      Used  Available Capacity Mounted on\n"
    "/dev/disk1s5     488245288 100000000  380000000      21% /\n"
)
_DF_CRITICAL = (
    "Filesystem     1024-blocks      Used  Available Capacity Mounted on\n"
    "/dev/disk1s5     488245288 470000000   10000000      98% /\n"
)
_DF_ALL = (
    "Filesystem     1024-blocks      Used  Available Capacity Mounted on\n"
    "/dev/disk1s5     488245288 470000000   10000000      98% /\n"
    "/dev/disk1s2     488245288 200000000  280000000      42% /System/Volumes/Data\n"
    "tmpfs              8000000   1000000    7000000      13% /tmp\n"
)
_VM_STAT_HEALTHY = (
    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
    "Pages free:                              200000.\n"
    "Pages active:                            300000.\n"
    "Pages inactive:                          100000.\n"
    "Pages speculative:                        50000.\n"
    "Pages wired down:                        150000.\n"
    "Pages occupied by compressor:             50000.\n"
)
_VM_STAT_WARNING = (
    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
    "Pages free:                                5000.\n"
    "Pages active:                            500000.\n"
    "Pages inactive:                           10000.\n"
    "Pages speculative:                         1000.\n"
    "Pages wired down:                        200000.\n"
    "Pages occupied by compressor:            100000.\n"
)
_FREE_HEALTHY = (
    "              total        used        free      shared  buff/cache   available\n"
    "Mem:    16000000000  4000000000  5000000000   100000000  7000000000 11000000000\n"
    "Swap:   2000000000           0  2000000000\n"
)
_PS_HEALTHY = (
    "  PID STAT %CPU COMMAND\n"
    "    1 Ss    0.0 launchd\n"
    "  100 S     1.2 python\n"
    "  200 S     0.5 vim\n"
)
_PS_CRITICAL = (
    "  PID STAT %CPU COMMAND\n"
    "    1 Ss    0.0 launchd\n"
    "  100 Z+    0.0 dead_child_1\n"
    "  200 Z     0.0 dead_child_2\n"
    "  300 Z     0.0 dead_child_3\n"
    "  400 S    80.5 python\n"
)
_PS_ZOMBIES = (
    "  PID  PPID STAT COMMAND\n"
    "  100     1 Z    dead_child_1\n"
    "  200    50 Z+   dead_child_2\n"
    "  300    60 Z    dead_child_3\n"
)
_GETCONF_8 = "8\n"
_SYSCTL_PAGESIZE = "4096\n"


# ── parser unit tests ──────────────────────────────────────────────


def test_parse_uptime_handles_comma_and_space_separators():
    a = _parse_uptime("12:34 up 1 day, 1:00, 1 user, load average: 0.42, 0.51, 0.61\n")
    assert a == {"load_1min": 0.42, "load_5min": 0.51, "load_15min": 0.61}
    b = _parse_uptime("up 1 day, load averages: 1.10 2.20 3.30\n")
    assert b == {"load_1min": 1.10, "load_5min": 2.20, "load_15min": 3.30}


def test_parse_uptime_raises_on_malformed():
    with pytest.raises(ValueError, match="could not parse"):
        _parse_uptime("no load info here\n")


def test_parse_df_root_extracts_percentage_and_sizes():
    d = _parse_df_root(_DF_HEALTHY)
    assert d["percent"] == 21.0
    assert d["mountpoint"] == "/"
    assert d["filesystem"] == "/dev/disk1s5"
    assert d["total_gb"] > 0
    assert d["used_gb"] > 0
    assert d["free_gb"] > 0


def test_parse_df_all_returns_one_row_per_mount():
    rows = _parse_df_all(_DF_ALL)
    assert len(rows) == 3
    assert {r["mountpoint"] for r in rows} == {
        "/",
        "/System/Volumes/Data",
        "/tmp",
    }


def test_parse_vm_stat_computes_percent_used():
    d = _parse_vm_stat(_VM_STAT_HEALTHY, page_size=4096)
    # used = 300000 + 150000 + 50000 = 500000
    # total = 200000 + 300000 + 100000 + 50000 + 150000 + 50000 = 850000
    # percent = 500000/850000*100 ~= 58.8
    assert 58.0 < d["percent"] < 60.0


def test_parse_free_extracts_mem_line():
    d = _parse_free(_FREE_HEALTHY)
    assert d["total_gb"] > 14.0
    assert 24.0 < d["percent"] < 26.0


def test_parse_ps_summary_counts_zombies_and_finds_top_cpu():
    d = _parse_ps_summary(_PS_CRITICAL)
    assert d["zombie_count"] == 3
    assert sorted(d["zombie_pids"]) == [100, 200, 300]
    assert d["top_cpu_pid"] == 400
    assert d["top_cpu_pct"] == 80.5


def test_parse_ps_rss_sorts_descending():
    raw = "  PID    RSS COMMAND\n  100    500 small\n  200  50000 big\n  300   1000 medium\n"
    rows = _parse_ps_rss(raw, top_n=3)
    assert [r["pid"] for r in rows] == [200, 300, 100]


def test_parse_ps_cpu_sorts_descending():
    raw = "  PID %CPU COMMAND\n  100  0.5 idle\n  200 99.0 hot\n  300  5.0 warm\n"
    rows = _parse_ps_cpu(raw, top_n=3)
    assert [r["pid"] for r in rows] == [200, 300, 100]


def test_parse_ps_zombies_filters_to_z_stat():
    raw = (
        "  PID  PPID STAT COMMAND\n"
        "  100     1 S    alive\n"
        "  200    50 Z+   dead\n"
        "  300    60 Z    also_dead\n"
    )
    zombies = _parse_ps_zombies(raw)
    assert len(zombies) == 2
    assert {z["pid"] for z in zombies} == {200, 300}


# ── severity classifier unit tests ────────────────────────────────


def test_severity_for_percent_thresholds():
    assert _severity_for(50.0, 80.0) == "ok"
    assert _severity_for(85.0, 80.0) == "warning"
    assert _severity_for(96.0, 80.0) == "critical"


def test_severity_for_load_doubles_to_critical():
    assert _severity_for_load(1.0, 2.0) == "ok"
    assert _severity_for_load(3.0, 2.0) == "warning"
    assert _severity_for_load(4.0, 2.0) == "critical"


def test_severity_for_zombies_step_function():
    assert _severity_for_zombies(0) == "ok"
    assert _severity_for_zombies(1) == "warning"
    assert _severity_for_zombies(3) == "critical"


def test_max_severity_returns_worst_label():
    assert _max_severity(["ok", "ok", "ok"]) == "ok"
    assert _max_severity(["ok", "warning", "ok"]) == "warning"
    assert _max_severity(["warning", "critical", "ok"]) == "critical"


# ── configure validation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_configure_rejects_negative_disk_pct():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "configure", "inputs": {"disk_pct": -1}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert "disk_pct" in out["error_message"]


@pytest.mark.asyncio
async def test_configure_rejects_memory_pct_over_100():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "configure", "inputs": {"memory_pct": 150}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"


# ── full-walk tests (monkey-patched shellouts) ────────────────────


@pytest.mark.asyncio
async def test_walk_to_produce_report_on_clean_host(monkeypatch):
    """Healthy disk/memory/load/processes -> produce_report path."""
    _patch_runner(
        monkeypatch,
        {
            ("df", "-k", "/"): _DF_HEALTHY,
            ("sysctl", "-n", "hw.pagesize"): _SYSCTL_PAGESIZE,
            ("vm_stat",): _VM_STAT_HEALTHY,
            ("free", "-b"): _FREE_HEALTHY,
            ("uptime",): _UPTIME_HEALTHY,
            ("getconf", "_NPROCESSORS_ONLN"): _GETCONF_8,
            ("ps", "-A", "-o", "pid,stat,pcpu,comm"): _PS_HEALTHY,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        for act in [
            "check_disk",
            "check_memory",
            "check_load",
            "check_processes",
            "aggregate",
            "triage",
        ]:
            await client.call_tool("step", {"action": act, "inputs": {}})
        r = await client.call_tool("step", {"action": "produce_report", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["overall_severity"] == "healthy"
        assert out["state"]["report"]["overall_severity"] == "healthy"
        for key in ("disk", "memory", "load", "processes"):
            assert key in out["state"]["raw_outputs"]


@pytest.mark.asyncio
async def test_walk_to_raise_alert_when_disk_critical(monkeypatch):
    """Disk at 98% triggers critical, routes through deep_dive."""
    _patch_runner(
        monkeypatch,
        {
            ("df", "-k", "/"): _DF_CRITICAL,
            ("df", "-k"): _DF_ALL,
            ("sysctl", "-n", "hw.pagesize"): _SYSCTL_PAGESIZE,
            ("vm_stat",): _VM_STAT_HEALTHY,
            ("free", "-b"): _FREE_HEALTHY,
            ("uptime",): _UPTIME_HEALTHY,
            ("getconf", "_NPROCESSORS_ONLN"): _GETCONF_8,
            ("ps", "-A", "-o", "pid,stat,pcpu,comm"): _PS_HEALTHY,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        for act in [
            "check_disk",
            "check_memory",
            "check_load",
            "check_processes",
            "aggregate",
            "triage",
        ]:
            await client.call_tool("step", {"action": act, "inputs": {}})
        r = await client.call_tool("step", {"action": "deep_dive", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["overall_severity"] == "critical"
        assert out["state"]["worst_subsystem"] == "disk"
        assert "top_mounts" in out["state"]["deep_dive"]
        r = await client.call_tool("step", {"action": "raise_alert", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["report"]["overall_severity"] == "critical"
        assert out["state"]["report"]["worst_subsystem"] == "disk"


@pytest.mark.asyncio
async def test_walk_to_raise_alert_when_zombies_critical(monkeypatch):
    """3+ zombies forces processes-subsystem critical."""
    _patch_runner(
        monkeypatch,
        {
            ("df", "-k", "/"): _DF_HEALTHY,
            ("sysctl", "-n", "hw.pagesize"): _SYSCTL_PAGESIZE,
            ("vm_stat",): _VM_STAT_HEALTHY,
            ("free", "-b"): _FREE_HEALTHY,
            ("uptime",): _UPTIME_HEALTHY,
            ("getconf", "_NPROCESSORS_ONLN"): _GETCONF_8,
            ("ps", "-A", "-o", "pid,stat,pcpu,comm"): _PS_CRITICAL,
            ("ps", "-A", "-o", "pid,ppid,stat,comm"): _PS_ZOMBIES,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        for act in [
            "check_disk",
            "check_memory",
            "check_load",
            "check_processes",
            "aggregate",
            "triage",
        ]:
            await client.call_tool("step", {"action": act, "inputs": {}})
        r = await client.call_tool("step", {"action": "deep_dive", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["state"]["worst_subsystem"] == "processes"
        assert len(out["state"]["deep_dive"]["zombies"]) == 3


# ── transition gating ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_to_produce_report_invalid_when_critical(monkeypatch):
    _patch_runner(
        monkeypatch,
        {
            ("df", "-k", "/"): _DF_CRITICAL,
            ("sysctl", "-n", "hw.pagesize"): _SYSCTL_PAGESIZE,
            ("vm_stat",): _VM_STAT_HEALTHY,
            ("free", "-b"): _FREE_HEALTHY,
            ("uptime",): _UPTIME_HEALTHY,
            ("getconf", "_NPROCESSORS_ONLN"): _GETCONF_8,
            ("ps", "-A", "-o", "pid,stat,pcpu,comm"): _PS_HEALTHY,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        for act in [
            "check_disk",
            "check_memory",
            "check_load",
            "check_processes",
            "aggregate",
            "triage",
        ]:
            await client.call_tool("step", {"action": act, "inputs": {}})
        r = await client.call_tool("step", {"action": "produce_report", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["deep_dive"]


@pytest.mark.asyncio
async def test_raw_outputs_recorded_verbatim(monkeypatch):
    """state.raw_outputs holds the literal stdout of each tool, so an
    operator can see what the FSM actually ran."""
    _patch_runner(
        monkeypatch,
        {
            ("df", "-k", "/"): _DF_HEALTHY,
            ("sysctl", "-n", "hw.pagesize"): _SYSCTL_PAGESIZE,
            ("vm_stat",): _VM_STAT_HEALTHY,
            ("free", "-b"): _FREE_HEALTHY,
            ("uptime",): _UPTIME_HEALTHY,
            ("getconf", "_NPROCESSORS_ONLN"): _GETCONF_8,
            ("ps", "-A", "-o", "pid,stat,pcpu,comm"): _PS_HEALTHY,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        await client.call_tool("step", {"action": "check_disk", "inputs": {}})
        r = await client.call_tool("step", {"action": "check_memory", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert "Filesystem" in out["state"]["raw_outputs"]["disk"]
        assert "/dev/disk1s5" in out["state"]["raw_outputs"]["disk"]


# ── shellout failure modes ───────────────────────────────────────


@pytest.mark.asyncio
async def test_check_disk_surfaces_command_failure(monkeypatch):
    async def fake(args, timeout=10.0):
        raise FileNotFoundError("df not on PATH")

    monkeypatch.setattr(unix_health, "_run_check_command", fake)
    server = build_server()
    async with Client(server) as client:
        await client.call_tool("step", {"action": "configure", "inputs": {}})
        r = await client.call_tool("step", {"action": "check_disk", "inputs": {}})
        out = json.loads(r.content[0].text)
        assert out["error"] == "action_error"
        assert "df" in out["error_message"]
