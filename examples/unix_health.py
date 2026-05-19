"""Unix system-health triage as an FSM.

A pure-deterministic health check that walks four subsystems
(disk, memory, load, processes) and routes to a clean report
or a critical alert depending on what it finds:

    configure -> check_disk -> check_memory -> check_load
                                                   |
                                            check_processes
                                                   |
                                              aggregate
                                                   |
                                                triage
                                              /        \\
                  (healthy / warning)        /          \\        (critical)
                                            v            v
                                    produce_report     deep_dive
                                                          |
                                                     raise_alert

What the FSM gives the health check:

* Every subsystem probe is its own visible step in
  ``burr://history`` and ``burr://trace``. A timeline of which
  check fired what severity is reconstructable from the trace
  without bolting a separate metrics-logger on.
* The deep-dive branch is encoded as a transition condition, not
  as ``if overall == 'critical': do_more_work`` inside a single
  function. An agent reading ``burr://next`` after ``triage`` sees
  exactly one valid forward move; the routing decision lives in
  the graph instead of in glue code.
* The escalation primitive (deep_dive -> raise_alert) collects
  subsystem-specific extra detail (top processes, mount usage,
  zombie parents) only when something is actually wrong, so the
  fast-path report stays cheap.
* Non-LLM. Pure psutil probes plus stdlib. Suitable as a smoke
  test for FSM-mediated branching where the routing condition is
  data-derived rather than agent-decided.

Thresholds default to disk_pct=80.0, memory_pct=85.0,
load_per_cpu=2.0 and can be overridden via ``configure``.

Run:

    python examples/unix_health.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psutil
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount

_TRACKER_PROJECT = "unix-health-demo"

_SEVERITY_ORDER = {"ok": 0, "warning": 1, "critical": 2}
_SEVERITY_NAME = {0: "ok", 1: "warning", 2: "critical"}

_SUGGESTED_ACTIONS = {
    "disk": "Free space on the saturated mountpoint or expand the volume.",
    "memory": "Restart leak-prone services or add swap; investigate top RSS consumers.",
    "load": "Identify runaway CPU consumers; throttle or kill them.",
    "processes": "Reap zombies by restarting or fixing their parent processes.",
}


# tiny helpers (pure stdlib + psutil) ────────────────────────────────


def _bytes_to_gb(n: int) -> float:
    return round(n / (1024**3), 2)


def _severity_for(percent: float, warning_at: float) -> str:
    """Map a percent value to a three-level severity."""
    if percent >= 95.0:
        return "critical"
    if percent >= warning_at:
        return "warning"
    return "ok"


def _severity_for_load(per_cpu: float, warning_at: float) -> str:
    if per_cpu >= 2 * warning_at:
        return "critical"
    if per_cpu >= warning_at:
        return "warning"
    return "ok"


def _severity_for_zombies(count: int) -> str:
    if count >= 3:
        return "critical"
    if count >= 1:
        return "warning"
    return "ok"


def _max_severity(severities: list[str]) -> str:
    return _SEVERITY_NAME[max(_SEVERITY_ORDER[s] for s in severities)]


# actions ────────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "thresholds",
        "checks",
        "overall_severity",
        "worst_subsystem",
        "deep_dive",
        "report",
        "log",
    ],
)
def configure(
    state: State,
    disk_pct: float = 80.0,
    memory_pct: float = 85.0,
    load_per_cpu: float = 2.0,
) -> State:
    """Set the warning thresholds for the four subsystem checks.

    Args:
        disk_pct: Disk-usage percent at which the disk check
            transitions from ok to warning. The critical cutoff
            is fixed at 95 percent. Must be in (0, 100].
        memory_pct: Memory-usage percent at which the memory
            check transitions from ok to warning. Critical at 95.
            Must be in (0, 100].
        load_per_cpu: 1-minute load average divided by CPU count
            at which the load check transitions from ok to
            warning. Critical at 2x this value. Must be positive.
    """
    if disk_pct <= 0:
        raise ValueError("disk_pct must be positive")
    if disk_pct > 100:
        raise ValueError("disk_pct must be <= 100")
    if memory_pct <= 0:
        raise ValueError("memory_pct must be positive")
    if memory_pct > 100:
        raise ValueError("memory_pct must be <= 100")
    if load_per_cpu <= 0:
        raise ValueError("load_per_cpu must be positive")
    thresholds = {
        "disk_pct": disk_pct,
        "memory_pct": memory_pct,
        "load_per_cpu": load_per_cpu,
    }
    return state.update(
        thresholds=thresholds,
        checks={},
        overall_severity="healthy",
        worst_subsystem=None,
        deep_dive=None,
        report=None,
        log=[
            f"Configured thresholds: disk_pct={disk_pct}, "
            f"memory_pct={memory_pct}, load_per_cpu={load_per_cpu}"
        ],
    )


@action(reads=["thresholds", "checks", "log"], writes=["checks", "log"])
def check_disk(state: State) -> State:
    """Probe root-filesystem usage and classify severity."""
    usage = psutil.disk_usage("/")
    percent = float(usage.percent)
    status = _severity_for(percent, state["thresholds"]["disk_pct"])
    detail = {
        "path": "/",
        "total_gb": _bytes_to_gb(usage.total),
        "used_gb": _bytes_to_gb(usage.used),
        "free_gb": _bytes_to_gb(usage.free),
        "percent": percent,
    }
    checks = {**state["checks"], "disk": {"status": status, "detail": detail}}
    return state.update(
        checks=checks,
        log=[*state["log"], f"check_disk: status={status} percent={percent}"],
    )


@action(reads=["thresholds", "checks", "log"], writes=["checks", "log"])
def check_memory(state: State) -> State:
    """Probe virtual-memory usage and classify severity."""
    vm = psutil.virtual_memory()
    percent = float(vm.percent)
    status = _severity_for(percent, state["thresholds"]["memory_pct"])
    detail = {
        "total_gb": _bytes_to_gb(vm.total),
        "available_gb": _bytes_to_gb(vm.available),
        "percent": percent,
    }
    checks = {**state["checks"], "memory": {"status": status, "detail": detail}}
    return state.update(
        checks=checks,
        log=[*state["log"], f"check_memory: status={status} percent={percent}"],
    )


@action(reads=["thresholds", "checks", "log"], writes=["checks", "log"])
def check_load(state: State) -> State:
    """Probe 1-minute load average normalized by CPU count."""
    load_1min = float(os.getloadavg()[0])
    cpu_count = psutil.cpu_count() or 1
    per_cpu = round(load_1min / cpu_count, 4)
    status = _severity_for_load(per_cpu, state["thresholds"]["load_per_cpu"])
    detail = {
        "load_1min": load_1min,
        "cpu_count": cpu_count,
        "per_cpu": per_cpu,
    }
    checks = {**state["checks"], "load": {"status": status, "detail": detail}}
    return state.update(
        checks=checks,
        log=[*state["log"], f"check_load: status={status} per_cpu={per_cpu}"],
    )


@action(reads=["checks", "log"], writes=["checks", "log"])
def check_processes(state: State) -> State:
    """Count zombies and identify the top CPU consumer."""
    zombie_pids: list[int] = []
    top_cpu_pid: int | None = None
    top_cpu_pct = -1.0
    top_cpu_name: str | None = None
    for proc in psutil.process_iter(attrs=["pid", "name", "status", "cpu_percent"]):
        info = proc.info
        if info.get("status") == psutil.STATUS_ZOMBIE:
            zombie_pids.append(int(info["pid"]))
        cpu_pct = info.get("cpu_percent") or 0.0
        if cpu_pct > top_cpu_pct:
            top_cpu_pct = float(cpu_pct)
            top_cpu_pid = int(info["pid"]) if info.get("pid") is not None else None
            top_cpu_name = info.get("name")
    status = _severity_for_zombies(len(zombie_pids))
    detail = {
        "zombie_count": len(zombie_pids),
        "zombie_pids": zombie_pids[:5],
        "top_cpu_pid": top_cpu_pid,
        "top_cpu_pct": round(top_cpu_pct, 2) if top_cpu_pct >= 0 else 0.0,
        "top_cpu_name": top_cpu_name,
    }
    checks = {**state["checks"], "processes": {"status": status, "detail": detail}}
    return state.update(
        checks=checks,
        log=[*state["log"], f"check_processes: status={status} zombies={len(zombie_pids)}"],
    )


@action(
    reads=["checks", "log"],
    writes=["overall_severity", "worst_subsystem", "log"],
)
def aggregate(state: State) -> State:
    """Roll subsystem severities into an overall severity and pick the worst."""
    checks = state["checks"]
    order = ["disk", "memory", "load", "processes"]
    severities = [checks[name]["status"] for name in order]
    worst = _max_severity(severities)
    worst_rank = _SEVERITY_ORDER[worst]
    worst_subsystem: str | None = None
    if worst_rank > 0:
        for name in order:
            if _SEVERITY_ORDER[checks[name]["status"]] == worst_rank:
                worst_subsystem = name
                break
    overall = "healthy" if worst == "ok" else worst
    return state.update(
        overall_severity=overall,
        worst_subsystem=worst_subsystem,
        log=[*state["log"], f"aggregate: overall={overall} worst_subsystem={worst_subsystem}"],
    )


@action(reads=["overall_severity", "worst_subsystem", "log"], writes=["log"])
def triage(state: State) -> State:
    """Routing node; logs the chosen branch but writes no decisions."""
    overall = state["overall_severity"]
    worst = state["worst_subsystem"]
    return state.update(
        log=[*state["log"], f"triage: overall={overall} worst_subsystem={worst}"],
    )


@action(reads=["worst_subsystem", "checks", "log"], writes=["deep_dive", "log"])
def deep_dive(state: State) -> State:
    """Gather subsystem-specific extra detail for the worst offender."""
    worst = state["worst_subsystem"]
    extras: dict[str, Any] = {"subsystem": worst}
    if worst == "disk":
        mounts: list[dict[str, Any]] = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            mounts.append(
                {
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "percent": float(usage.percent),
                    "used_gb": _bytes_to_gb(usage.used),
                    "total_gb": _bytes_to_gb(usage.total),
                }
            )
        mounts.sort(key=lambda m: m["percent"], reverse=True)
        extras["top_mounts"] = mounts[:5]
    elif worst == "memory":
        ranked: list[dict[str, Any]] = []
        for proc in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
            info = proc.info
            mem_info = info.get("memory_info")
            rss = getattr(mem_info, "rss", 0) if mem_info is not None else 0
            ranked.append(
                {
                    "pid": int(info["pid"]) if info.get("pid") is not None else None,
                    "name": info.get("name"),
                    "rss_mb": round(rss / (1024**2), 2),
                }
            )
        ranked.sort(key=lambda r: r["rss_mb"], reverse=True)
        extras["top_memory"] = ranked[:5]
    elif worst == "load":
        ranked = []
        for proc in psutil.process_iter(attrs=["pid", "name", "cpu_percent"]):
            info = proc.info
            ranked.append(
                {
                    "pid": int(info["pid"]) if info.get("pid") is not None else None,
                    "name": info.get("name"),
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                }
            )
        ranked.sort(key=lambda r: r["cpu_percent"], reverse=True)
        extras["top_cpu"] = ranked[:5]
    elif worst == "processes":
        zombies: list[dict[str, Any]] = []
        for proc in psutil.process_iter(attrs=["pid", "name", "status", "ppid"]):
            info = proc.info
            if info.get("status") == psutil.STATUS_ZOMBIE:
                zombies.append(
                    {
                        "pid": int(info["pid"]) if info.get("pid") is not None else None,
                        "name": info.get("name"),
                        "ppid": int(info["ppid"]) if info.get("ppid") is not None else None,
                    }
                )
        extras["zombies"] = zombies
    return state.update(
        deep_dive=extras,
        log=[*state["log"], f"deep_dive: subsystem={worst} keys={sorted(extras.keys())}"],
    )


@action(
    reads=["overall_severity", "checks", "log"],
    writes=["report", "log"],
)
def produce_report(state: State) -> State:
    """Terminal: assemble a clean health report (no deep dive)."""
    overall = state["overall_severity"]
    checks = state["checks"]
    detail_bits = [f"{name}={info['status']}" for name, info in checks.items()]
    summary_line = f"system {overall}: {', '.join(detail_bits)}"
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "overall_severity": overall,
        "checks": checks,
        "summary_line": summary_line,
    }
    return state.update(
        report=report,
        log=[*state["log"], f"produce_report: overall={overall}"],
    )


@action(
    reads=["checks", "worst_subsystem", "deep_dive", "log"],
    writes=["report", "log"],
)
def raise_alert(state: State) -> State:
    """Terminal: assemble a critical-severity alert with deep-dive context."""
    worst = state["worst_subsystem"]
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "overall_severity": "critical",
        "checks": state["checks"],
        "worst_subsystem": worst,
        "deep_dive": state["deep_dive"],
        "suggested_actions": dict(_SUGGESTED_ACTIONS),
    }
    return state.update(
        report=report,
        log=[*state["log"], f"raise_alert: subsystem={worst}"],
    )


# graph ──────────────────────────────────────────────────────────────


_OK_OR_WARN = Condition.expr("overall_severity in ('healthy', 'warning')")
_CRITICAL = Condition.expr("overall_severity == 'critical'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            configure=configure,
            check_disk=check_disk,
            check_memory=check_memory,
            check_load=check_load,
            check_processes=check_processes,
            aggregate=aggregate,
            triage=triage,
            deep_dive=deep_dive,
            produce_report=produce_report,
            raise_alert=raise_alert,
        )
        .with_transitions(
            ("configure", "check_disk"),
            ("check_disk", "check_memory"),
            ("check_memory", "check_load"),
            ("check_load", "check_processes"),
            ("check_processes", "aggregate"),
            ("aggregate", "triage"),
            ("triage", "produce_report", _OK_OR_WARN),
            ("triage", "deep_dive", _CRITICAL),
            ("deep_dive", "raise_alert"),
            # produce_report and raise_alert are terminal.
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            thresholds={
                "disk_pct": 80.0,
                "memory_pct": 85.0,
                "load_per_cpu": 2.0,
            },
            checks={},
            overall_severity="healthy",
            worst_subsystem=None,
            deep_dive=None,
            report=None,
            log=[],
        )
        .with_entrypoint("configure")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="unix-health",
        instructions=(
            "Pure-deterministic Unix system-health FSM. Walks four "
            "subsystem probes (disk, memory, load, processes) then "
            "triages severity. Start every session with "
            "configure(disk_pct, memory_pct, load_per_cpu) (all "
            "optional; defaults 80.0 / 85.0 / 2.0). Walk: configure "
            "-> check_disk -> check_memory -> check_load -> "
            "check_processes -> aggregate -> triage, then either "
            "produce_report (terminal, when overall_severity is "
            "healthy or warning) or deep_dive -> raise_alert "
            "(terminal, when at least one check is critical). The "
            "deep_dive branch is reachable from triage only when "
            "overall_severity == 'critical'. Thresholds default to "
            "disk_pct=80.0, memory_pct=85.0, load_per_cpu=2.0 and "
            "can be overridden via configure args. No LLM calls."
        ),
    )


if __name__ == "__main__":
    build_server().run()
