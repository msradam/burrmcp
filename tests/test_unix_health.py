"""Unix-health FSM: deterministic branching demo.

Tests cover the happy path through to ``produce_report`` (healthy and
warning), the critical branch through ``deep_dive`` to
``raise_alert``, validation refusals from ``configure``, and FSM
gating that makes the wrong post-triage transition invalid.

The host running the tests may itself be perfectly healthy or under
load, so every test monkeypatches ``psutil`` plus ``os.getloadavg``
to feed fixed values into the four subsystem probes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import unix_health  # noqa: E402  isort:skip
from unix_health import build_server  # noqa: E402  isort:skip


# fixture helpers ────────────────────────────────────────────────────


class _NS:
    """Tiny attribute namespace; psutil returns named-tuple-like objects."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_proc(
    pid: int,
    name: str = "fake",
    status: str = "running",
    cpu_percent: float = 0.0,
    rss: int = 0,
    ppid: int = 1,
):
    """Build a fake psutil.Process-shaped object.

    ``process_iter(attrs=...)`` returns objects whose ``.info`` dict has
    the requested keys. The shim exposes both ``.info`` (used by the
    action code) and a ``.ppid()`` callable for the zombie deep-dive
    path which does ``psutil.Process(pid).ppid()``.
    """
    info = {
        "pid": pid,
        "name": name,
        "status": status,
        "cpu_percent": cpu_percent,
        "memory_info": _NS(rss=rss),
        "ppid": ppid,
    }
    proc = _NS(info=info, pid=pid)
    proc.ppid = lambda: ppid
    return proc


def _patch_health(
    monkeypatch,
    *,
    disk_pct: float = 30.0,
    mem_pct: float = 40.0,
    load_1min: float = 0.5,
    cpu_count: int = 4,
    zombies: int = 0,
    top_cpu_pct: float = 10.0,
):
    """Pin the four subsystem probes to deterministic values."""
    total_disk = 10**12
    monkeypatch.setattr(
        unix_health.psutil,
        "disk_usage",
        lambda p: _NS(
            total=total_disk,
            used=int(total_disk * disk_pct / 100),
            free=int(total_disk * (1 - disk_pct / 100)),
            percent=disk_pct,
        ),
    )
    total_mem = 10**10
    monkeypatch.setattr(
        unix_health.psutil,
        "virtual_memory",
        lambda: _NS(
            total=total_mem,
            available=int(total_mem * (1 - mem_pct / 100)),
            percent=mem_pct,
        ),
    )
    monkeypatch.setattr(unix_health.psutil, "cpu_count", lambda: cpu_count)
    monkeypatch.setattr(unix_health.os, "getloadavg", lambda: (load_1min, 0.0, 0.0))

    # process_iter: one busy process, plus N zombies, plus a quiet one.
    fake_procs = [
        _fake_proc(pid=1001, name="busy", cpu_percent=top_cpu_pct, rss=500 * 1024 * 1024),
        _fake_proc(pid=1002, name="idle", cpu_percent=0.1, rss=10 * 1024 * 1024),
    ]
    for i in range(zombies):
        fake_procs.append(
            _fake_proc(
                pid=2000 + i,
                name=f"zombie_{i}",
                status=unix_health.psutil.STATUS_ZOMBIE,
                ppid=1,
            )
        )
    monkeypatch.setattr(
        unix_health.psutil,
        "process_iter",
        lambda attrs=None: iter(fake_procs),
    )

    # disk_partitions for deep_dive on disk; one fake mountpoint, /.
    monkeypatch.setattr(
        unix_health.psutil,
        "disk_partitions",
        lambda all=False: [_NS(mountpoint="/", fstype="apfs", device="/dev/disk1")],
    )


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


async def _walk_through_checks(client):
    """configure -> ... -> aggregate -> triage. Returns the last payload."""
    await _step(client, "configure")
    await _step(client, "check_disk")
    await _step(client, "check_memory")
    await _step(client, "check_load")
    await _step(client, "check_processes")
    await _step(client, "aggregate")
    return _payload(await _step(client, "triage"))


# configure validation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_configure_with_defaults_succeeds(monkeypatch):
    _patch_health(monkeypatch)
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure"))
        assert out["action"] == "configure"
        assert out["state"]["thresholds"] == {
            "disk_pct": 80.0,
            "memory_pct": 85.0,
            "load_per_cpu": 2.0,
        }


@pytest.mark.asyncio
async def test_configure_rejects_negative_disk_pct(monkeypatch):
    _patch_health(monkeypatch)
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", disk_pct=-1))
        assert out["error"] == "action_error"
        assert "disk_pct" in out["error_message"]


@pytest.mark.asyncio
async def test_configure_rejects_memory_pct_over_100(monkeypatch):
    _patch_health(monkeypatch)
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", memory_pct=150))
        assert out["error"] == "action_error"
        assert "memory_pct" in out["error_message"]


# happy-path walks ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_ok_walks_through_to_produce_report(monkeypatch):
    _patch_health(monkeypatch, disk_pct=30, mem_pct=40, load_1min=0.5, zombies=0)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "healthy"
        assert out["valid_next_actions"] == ["produce_report"]
        out = _payload(await _step(client, "produce_report"))
        report = out["state"]["report"]
        assert report["overall_severity"] == "healthy"
        assert "timestamp_utc" in report
        assert report["summary_line"].startswith("system healthy:")


@pytest.mark.asyncio
async def test_disk_critical_routes_through_deep_dive_to_raise_alert(monkeypatch):
    _patch_health(monkeypatch, disk_pct=99, mem_pct=40, load_1min=0.5)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "critical"
        assert out["state"]["worst_subsystem"] == "disk"
        assert out["valid_next_actions"] == ["deep_dive"]
        out = _payload(await _step(client, "deep_dive"))
        deep = out["state"]["deep_dive"]
        assert deep["subsystem"] == "disk"
        assert "top_mounts" in deep
        assert isinstance(deep["top_mounts"], list)
        assert len(deep["top_mounts"]) >= 1
        out = _payload(await _step(client, "raise_alert"))
        report = out["state"]["report"]
        assert report["overall_severity"] == "critical"
        assert report["worst_subsystem"] == "disk"
        assert "suggested_actions" in report
        assert report["deep_dive"]["subsystem"] == "disk"


@pytest.mark.asyncio
async def test_three_zombies_route_through_processes_deep_dive(monkeypatch):
    _patch_health(monkeypatch, disk_pct=30, mem_pct=40, load_1min=0.5, zombies=3)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "critical"
        assert out["state"]["worst_subsystem"] == "processes"
        out = _payload(await _step(client, "deep_dive"))
        deep = out["state"]["deep_dive"]
        assert deep["subsystem"] == "processes"
        assert "zombies" in deep
        assert len(deep["zombies"]) == 3
        assert all("ppid" in z for z in deep["zombies"])


@pytest.mark.asyncio
async def test_one_warning_rest_ok_produces_warning_report(monkeypatch):
    # disk 88 trips warning (>=80), critical is 95; everything else ok.
    _patch_health(monkeypatch, disk_pct=88, mem_pct=40, load_1min=0.5, zombies=0)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "warning"
        assert out["state"]["worst_subsystem"] == "disk"
        assert out["valid_next_actions"] == ["produce_report"]
        out = _payload(await _step(client, "produce_report"))
        assert out["state"]["report"]["overall_severity"] == "warning"


@pytest.mark.asyncio
async def test_one_critical_load_routes_through_deep_dive(monkeypatch):
    # load_per_cpu = 10/4 = 2.5; threshold 2.0; 2x = 4. 2.5 is warning,
    # not critical. Push load to 20 so per_cpu = 5 > 4 -> critical.
    _patch_health(monkeypatch, disk_pct=30, mem_pct=40, load_1min=20, cpu_count=4)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "critical"
        assert out["state"]["worst_subsystem"] == "load"
        assert out["valid_next_actions"] == ["deep_dive"]
        out = _payload(await _step(client, "deep_dive"))
        assert out["state"]["deep_dive"]["subsystem"] == "load"
        assert "top_cpu" in out["state"]["deep_dive"]


# deterministic worst-subsystem selection ────────────────────────────


@pytest.mark.asyncio
async def test_worst_subsystem_picks_first_failing_in_order(monkeypatch):
    # Disk and memory both critical; spec says order is disk, memory,
    # load, processes, so disk wins.
    _patch_health(monkeypatch, disk_pct=99, mem_pct=99, load_1min=0.5, zombies=0)
    server = build_server()
    async with Client(server) as client:
        out = await _walk_through_checks(client)
        assert out["state"]["overall_severity"] == "critical"
        assert out["state"]["worst_subsystem"] == "disk"


# FSM gating on the triage branch ────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_to_produce_report_invalid_when_critical(monkeypatch):
    _patch_health(monkeypatch, disk_pct=99, zombies=0)
    server = build_server()
    async with Client(server) as client:
        await _walk_through_checks(client)
        out = _payload(await _step(client, "produce_report"))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["deep_dive"]


@pytest.mark.asyncio
async def test_triage_to_deep_dive_invalid_when_warning(monkeypatch):
    # disk 88 -> warning, not critical. deep_dive must refuse.
    _patch_health(monkeypatch, disk_pct=88, mem_pct=40, load_1min=0.5, zombies=0)
    server = build_server()
    async with Client(server) as client:
        await _walk_through_checks(client)
        out = _payload(await _step(client, "deep_dive"))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["produce_report"]


# individual check_* actions populate state correctly ────────────────


@pytest.mark.asyncio
async def test_each_check_writes_status_and_detail(monkeypatch):
    _patch_health(monkeypatch, disk_pct=30, mem_pct=40, load_1min=0.5, zombies=0)
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure")
        out = _payload(await _step(client, "check_disk"))
        disk = out["state"]["checks"]["disk"]
        assert disk["status"] == "ok"
        assert set(disk["detail"]) >= {"path", "total_gb", "used_gb", "free_gb", "percent"}

        out = _payload(await _step(client, "check_memory"))
        mem = out["state"]["checks"]["memory"]
        assert mem["status"] == "ok"
        assert set(mem["detail"]) >= {"total_gb", "available_gb", "percent"}

        out = _payload(await _step(client, "check_load"))
        load = out["state"]["checks"]["load"]
        assert load["status"] == "ok"
        assert set(load["detail"]) >= {"load_1min", "cpu_count", "per_cpu"}

        out = _payload(await _step(client, "check_processes"))
        procs = out["state"]["checks"]["processes"]
        assert procs["status"] == "ok"
        assert set(procs["detail"]) >= {
            "zombie_count",
            "zombie_pids",
            "top_cpu_pid",
            "top_cpu_pct",
            "top_cpu_name",
        }
