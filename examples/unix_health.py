"""Unix system-health triage as an FSM, via real shell commands.

A deterministic health check that walks four subsystems (disk,
memory, load, processes) by shelling out to the canonical Unix
tools (``df``, ``uptime``, ``ps``, plus ``vm_stat`` on macOS or
``free`` on Linux), parses each tool's stdout, and routes to a
clean report or a critical alert depending on what it finds:

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

* Every subsystem probe is a real ``subprocess.exec`` of a tool an
  ops engineer would actually type. The raw stdout per check is
  recorded in ``state.raw_outputs`` so ``theodosia://state`` shows the
  literal output an operator would have seen at the terminal, not
  a pre-parsed dict.
* Every probe is its own visible step in ``theodosia://history`` and
  ``theodosia://trace``. A timeline of which command fired what
  severity is reconstructable from the trace without bolting a
  separate metrics-logger on.
* The deep-dive branch is encoded as a transition condition. An
  agent reading ``theodosia://next`` after ``triage`` sees exactly one
  valid forward move; the routing decision lives in the graph
  rather than in glue code.
* The escalation primitive (``deep_dive -> raise_alert``) runs
  subsystem-specific extra commands (top mounts, top processes by
  RSS or CPU, zombie reaper hunt) only when something is actually
  wrong, so the fast-path report stays cheap.
* Cross-platform on macOS (Darwin) and Linux. On other platforms
  the memory check will surface an ``action_error`` from the
  first shellout.

Thresholds default to disk_pct=80.0, memory_pct=85.0,
load_per_cpu=2.0 and can be overridden via ``configure``.

Run:

    python examples/unix_health.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "unix-health-demo"
_COMMAND_TIMEOUT = 10.0  # seconds per shellout

_SEVERITY_ORDER = {"ok": 0, "warning": 1, "critical": 2}
_SEVERITY_NAME = {0: "ok", 1: "warning", 2: "critical"}

_SUGGESTED_ACTIONS = {
    "disk": "Free space on the saturated mountpoint or expand the volume.",
    "memory": "Restart leak-prone services or add swap; investigate top RSS consumers.",
    "load": "Identify runaway CPU consumers; throttle or kill them.",
    "processes": "Reap zombies by restarting or fixing their parent processes.",
}


# ── shellout + parsers (pure functions; the FSM testbed monkey-patches the runner) ──


async def _run_check_command(args: list[str], timeout: float = _COMMAND_TIMEOUT) -> str:
    """Run a command via ``asyncio.create_subprocess_exec`` and return
    its stdout decoded as UTF-8.

    Raises ``FileNotFoundError`` if the tool is missing, ``ValueError``
    on non-zero exit or non-UTF-8 stdout, and ``asyncio.TimeoutError``
    on timeout. Tests monkey-patch this function so they stay fast and
    hermetic.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise ValueError(
            f"{args[0]} exited {proc.returncode}: {stderr_bytes.decode(errors='replace')[:200]}"
        )
    try:
        return stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return stdout_bytes.decode("utf-8", errors="replace")


def _parse_uptime(output: str) -> dict[str, Any]:
    """Pull the load averages out of ``uptime`` stdout.

    Format examples:
      "12:34:56 up 5 days,  2:13,  3 users,  load average: 0.42, 0.51, 0.61"
      "12:34:56  up 5 days,  2:13, 3 users, load averages: 0.42 0.51 0.61"
    """
    m = re.search(r"load averages?:\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", output)
    if not m:
        raise ValueError(f"could not parse uptime output: {output[:200]!r}")
    return {
        "load_1min": float(m.group(1)),
        "load_5min": float(m.group(2)),
        "load_15min": float(m.group(3)),
    }


def _parse_df_root(output: str) -> dict[str, Any]:
    """Parse a one-mount ``df -k <path>`` invocation.

    Format:
        Filesystem 1024-blocks Used Available Capacity Mounted on
        /dev/disk1s5  488245288 380000000 100000000 80% /
    BSD ``df`` may wrap long filesystem names onto two lines; we
    take the last line and locate the percentage column by suffix.
    """
    lines = output.strip().splitlines()
    if len(lines) < 2:
        raise ValueError(f"df output too short: {output[:200]!r}")
    parts = lines[-1].split()
    pct_idx = next((i for i, p in enumerate(parts) if p.endswith("%")), None)
    if pct_idx is None or pct_idx < 3:
        raise ValueError(f"no percentage column in df output: {output[:200]!r}")
    percent = float(parts[pct_idx].rstrip("%"))
    total_kb = int(parts[pct_idx - 3])
    used_kb = int(parts[pct_idx - 2])
    avail_kb = int(parts[pct_idx - 1])
    return {
        "filesystem": parts[0],
        "mountpoint": parts[-1],
        "total_gb": round(total_kb / (1024 * 1024), 2),
        "used_gb": round(used_kb / (1024 * 1024), 2),
        "free_gb": round(avail_kb / (1024 * 1024), 2),
        "percent": percent,
    }


def _parse_df_all(output: str) -> list[dict[str, Any]]:
    """Parse multi-mount ``df -k`` output into per-mount dicts."""
    rows: list[dict[str, Any]] = []
    for line in output.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 5:
            continue
        pct_idx = next((i for i, p in enumerate(parts) if p.endswith("%")), None)
        if pct_idx is None or pct_idx < 3:
            continue
        try:
            total_kb = int(parts[pct_idx - 3])
        except ValueError:
            continue
        rows.append(
            {
                "filesystem": parts[0],
                "mountpoint": parts[-1],
                "percent": float(parts[pct_idx].rstrip("%")),
                "used_gb": round(int(parts[pct_idx - 2]) / (1024 * 1024), 2),
                "total_gb": round(total_kb / (1024 * 1024), 2),
            }
        )
    return rows


_VM_STAT_LINE = re.compile(r'^"?([^":]+)"?:\s+(\d+)')


def _parse_vm_stat(output: str, page_size: int = 4096) -> dict[str, Any]:
    """Parse macOS ``vm_stat`` output into a memory-usage dict.

    Considers active + wired + compressed as "used"; free + inactive
    as "available" (macOS treats inactive pages as reclaimable).
    """
    counts: dict[str, int] = {}
    for line in output.splitlines():
        m = _VM_STAT_LINE.match(line.strip())
        if m:
            counts[m.group(1).strip()] = int(m.group(2))
    free = counts.get("Pages free", 0)
    active = counts.get("Pages active", 0)
    inactive = counts.get("Pages inactive", 0)
    wired = counts.get("Pages wired down", 0)
    compressed = counts.get("Pages occupied by compressor", 0)
    speculative = counts.get("Pages speculative", 0)
    total_pages = free + active + inactive + wired + compressed + speculative
    used_pages = active + wired + compressed
    if total_pages == 0:
        raise ValueError(f"vm_stat returned zero pages: {output[:200]!r}")
    percent = round(used_pages * 100 / total_pages, 1)
    total_bytes = total_pages * page_size
    avail_bytes = (free + inactive) * page_size
    return {
        "total_gb": round(total_bytes / (1024**3), 2),
        "available_gb": round(avail_bytes / (1024**3), 2),
        "percent": percent,
    }


def _parse_free(output: str) -> dict[str, Any]:
    """Parse Linux ``free -b`` output (header + Mem: line + Swap: line)."""
    mem_line = next((ln for ln in output.strip().splitlines() if ln.startswith("Mem:")), None)
    if not mem_line:
        raise ValueError(f"no Mem: line in free output: {output[:200]!r}")
    parts = mem_line.split()
    total = int(parts[1])
    used = int(parts[2])
    # ``free -b`` columns: total used free shared buff/cache available
    available = int(parts[6]) if len(parts) > 6 else int(parts[3])
    percent = round(used * 100 / total, 1) if total else 0.0
    return {
        "total_gb": round(total / (1024**3), 2),
        "available_gb": round(available / (1024**3), 2),
        "percent": percent,
    }


def _parse_ps_summary(output: str) -> dict[str, Any]:
    """Parse ``ps -A -o pid,stat,pcpu,comm`` for zombie count + top CPU."""
    zombie_pids: list[int] = []
    top_cpu_pid: int | None = None
    top_cpu_pct = -1.0
    top_cpu_name: str | None = None
    for line in output.strip().splitlines()[1:]:  # skip header
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            pcpu = float(parts[2])
        except ValueError:
            continue
        stat = parts[1]
        comm = parts[3]
        if "Z" in stat:
            zombie_pids.append(pid)
        if pcpu > top_cpu_pct:
            top_cpu_pct = pcpu
            top_cpu_pid = pid
            top_cpu_name = comm
    return {
        "zombie_count": len(zombie_pids),
        "zombie_pids": zombie_pids[:5],
        "top_cpu_pid": top_cpu_pid,
        "top_cpu_pct": round(top_cpu_pct, 2) if top_cpu_pct >= 0 else 0.0,
        "top_cpu_name": top_cpu_name,
    }


def _parse_ps_rss(output: str, top_n: int = 5) -> list[dict[str, Any]]:
    """Parse ``ps -A -o pid,rss,comm``; return top-``top_n`` by RSS desc."""
    rows: list[tuple[int, int, str]] = []
    for line in output.strip().splitlines()[1:]:
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])
        except ValueError:
            continue
        rows.append((pid, rss, parts[2]))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [
        {"pid": pid, "rss_mb": round(rss / 1024, 2), "name": name}
        for pid, rss, name in rows[:top_n]
    ]


def _parse_ps_cpu(output: str, top_n: int = 5) -> list[dict[str, Any]]:
    """Parse ``ps -A -o pid,pcpu,comm``; return top-``top_n`` by CPU desc."""
    rows: list[tuple[int, float, str]] = []
    for line in output.strip().splitlines()[1:]:
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pcpu = float(parts[1])
        except ValueError:
            continue
        rows.append((pid, pcpu, parts[2]))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [{"pid": pid, "cpu_percent": pcpu, "name": name} for pid, pcpu, name in rows[:top_n]]


def _parse_ps_zombies(output: str) -> list[dict[str, Any]]:
    """Parse ``ps -A -o pid,ppid,stat,comm``; return zombie processes only."""
    zombies: list[dict[str, Any]] = []
    for line in output.strip().splitlines()[1:]:
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        stat = parts[2]
        if "Z" in stat:
            zombies.append({"pid": pid, "ppid": ppid, "name": parts[3]})
    return zombies


# ── severity classifiers ────────────────────────────────────────────


def _severity_for(percent: float, warning_at: float) -> str:
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


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "thresholds",
        "checks",
        "raw_outputs",
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
        disk_pct: Percent at which the disk check transitions to
            warning. Critical cutoff is fixed at 95. Must be in
            (0, 100].
        memory_pct: Percent at which the memory check transitions
            to warning. Critical at 95. Must be in (0, 100].
        load_per_cpu: 1-minute load divided by core count at which
            the load check warns. Critical at 2x. Must be positive.
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
        raw_outputs={},
        overall_severity="healthy",
        worst_subsystem=None,
        deep_dive=None,
        report=None,
        log=[
            f"Configured thresholds: disk_pct={disk_pct}, "
            f"memory_pct={memory_pct}, load_per_cpu={load_per_cpu}"
        ],
    )


@action(
    reads=["thresholds", "checks", "raw_outputs", "log"],
    writes=["checks", "raw_outputs", "log"],
)
async def check_disk(state: State) -> State:
    """Run ``df -k /`` and classify root-filesystem usage."""
    raw = await _run_check_command(["df", "-k", "/"])
    detail = _parse_df_root(raw)
    status = _severity_for(detail["percent"], state["thresholds"]["disk_pct"])
    return state.update(
        checks={**state["checks"], "disk": {"status": status, "detail": detail}},
        raw_outputs={**state["raw_outputs"], "disk": raw},
        log=[*state["log"], f"check_disk: status={status} percent={detail['percent']}"],
    )


@action(
    reads=["thresholds", "checks", "raw_outputs", "log"],
    writes=["checks", "raw_outputs", "log"],
)
async def check_memory(state: State) -> State:
    """Run the platform-appropriate memory tool and classify usage.

    Linux: ``free -b``. macOS: ``vm_stat`` (we also pick up the page
    size from ``sysctl hw.pagesize``).
    """
    if sys.platform == "darwin":
        page_raw = await _run_check_command(["sysctl", "-n", "hw.pagesize"])
        try:
            page_size = int(page_raw.strip())
        except ValueError:
            page_size = 4096
        raw = await _run_check_command(["vm_stat"])
        detail = _parse_vm_stat(raw, page_size=page_size)
    else:
        raw = await _run_check_command(["free", "-b"])
        detail = _parse_free(raw)
    status = _severity_for(detail["percent"], state["thresholds"]["memory_pct"])
    return state.update(
        checks={**state["checks"], "memory": {"status": status, "detail": detail}},
        raw_outputs={**state["raw_outputs"], "memory": raw},
        log=[*state["log"], f"check_memory: status={status} percent={detail['percent']}"],
    )


@action(
    reads=["thresholds", "checks", "raw_outputs", "log"],
    writes=["checks", "raw_outputs", "log"],
)
async def check_load(state: State) -> State:
    """Run ``uptime`` and parse the load averages.

    Normalises the 1-minute load by core count (read from
    ``getconf _NPROCESSORS_ONLN``) before comparing to the
    per-CPU threshold.
    """
    raw = await _run_check_command(["uptime"])
    loads = _parse_uptime(raw)
    cpu_raw = await _run_check_command(["getconf", "_NPROCESSORS_ONLN"])
    try:
        cpu_count = max(1, int(cpu_raw.strip()))
    except ValueError:
        cpu_count = 1
    per_cpu = round(loads["load_1min"] / cpu_count, 4)
    detail = {**loads, "cpu_count": cpu_count, "per_cpu": per_cpu}
    status = _severity_for_load(per_cpu, state["thresholds"]["load_per_cpu"])
    return state.update(
        checks={**state["checks"], "load": {"status": status, "detail": detail}},
        raw_outputs={**state["raw_outputs"], "load": raw},
        log=[*state["log"], f"check_load: status={status} per_cpu={per_cpu}"],
    )


@action(
    reads=["checks", "raw_outputs", "log"],
    writes=["checks", "raw_outputs", "log"],
)
async def check_processes(state: State) -> State:
    """Run ``ps -A -o pid,stat,pcpu,comm`` to count zombies and find the
    top CPU consumer."""
    raw = await _run_check_command(["ps", "-A", "-o", "pid,stat,pcpu,comm"])
    detail = _parse_ps_summary(raw)
    status = _severity_for_zombies(detail["zombie_count"])
    return state.update(
        checks={**state["checks"], "processes": {"status": status, "detail": detail}},
        raw_outputs={**state["raw_outputs"], "processes": raw},
        log=[
            *state["log"],
            f"check_processes: status={status} zombies={detail['zombie_count']}",
        ],
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


@action(
    reads=["worst_subsystem", "raw_outputs", "log"],
    writes=["deep_dive", "raw_outputs", "log"],
)
async def deep_dive(state: State) -> State:
    """Run subsystem-specific follow-up commands and stash the parsed
    detail. Raw outputs of the follow-up commands are appended to
    ``state.raw_outputs`` under the ``deep_dive_*`` key prefix."""
    worst = state["worst_subsystem"]
    extras: dict[str, Any] = {"subsystem": worst}
    new_raw_outputs = dict(state["raw_outputs"])
    if worst == "disk":
        raw = await _run_check_command(["df", "-k"])
        new_raw_outputs["deep_dive_df"] = raw
        mounts = _parse_df_all(raw)
        mounts.sort(key=lambda m: m["percent"], reverse=True)
        extras["top_mounts"] = mounts[:5]
    elif worst == "memory":
        raw = await _run_check_command(["ps", "-A", "-o", "pid,rss,comm"])
        new_raw_outputs["deep_dive_ps_rss"] = raw
        extras["top_memory"] = _parse_ps_rss(raw, top_n=5)
    elif worst == "load":
        raw = await _run_check_command(["ps", "-A", "-o", "pid,pcpu,comm"])
        new_raw_outputs["deep_dive_ps_cpu"] = raw
        extras["top_cpu"] = _parse_ps_cpu(raw, top_n=5)
    elif worst == "processes":
        raw = await _run_check_command(["ps", "-A", "-o", "pid,ppid,stat,comm"])
        new_raw_outputs["deep_dive_ps_zombies"] = raw
        extras["zombies"] = _parse_ps_zombies(raw)
    return state.update(
        deep_dive=extras,
        raw_outputs=new_raw_outputs,
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
    return state.update(
        report={
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "overall_severity": overall,
            "checks": checks,
            "summary_line": f"system {overall}: {', '.join(detail_bits)}",
        },
        log=[*state["log"], f"produce_report: overall={overall}"],
    )


@action(
    reads=["checks", "worst_subsystem", "deep_dive", "log"],
    writes=["report", "log"],
)
def raise_alert(state: State) -> State:
    """Terminal: assemble a critical-severity alert with deep-dive context."""
    worst = state["worst_subsystem"]
    return state.update(
        report={
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "overall_severity": "critical",
            "checks": state["checks"],
            "worst_subsystem": worst,
            "deep_dive": state["deep_dive"],
            "suggested_actions": dict(_SUGGESTED_ACTIONS),
        },
        log=[*state["log"], f"raise_alert: subsystem={worst}"],
    )


# ── graph ───────────────────────────────────────────────────────────


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
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            thresholds={
                "disk_pct": 80.0,
                "memory_pct": 85.0,
                "load_per_cpu": 2.0,
            },
            checks={},
            raw_outputs={},
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
        name="burr-unix-health",
        instructions=(
            "Unix system-health FSM. Walks df, vm_stat or free, "
            "uptime, and ps via real subprocess invocations. Start "
            "every session with configure(disk_pct, memory_pct, "
            "load_per_cpu) (all optional; defaults 80.0 / 85.0 / "
            "2.0). Walk: configure -> check_disk -> check_memory -> "
            "check_load -> check_processes -> aggregate -> triage, "
            "then either produce_report (terminal, when "
            "overall_severity is healthy or warning) or deep_dive -> "
            "raise_alert (terminal, when at least one check is "
            "critical). Raw command output per check is recorded in "
            "state.raw_outputs so theodosia://state shows the literal "
            "stdout an operator would see at the terminal. Supports "
            "macOS (Darwin) and Linux; Windows users via WSL."
        ),
    )


if __name__ == "__main__":
    build_server().run()
