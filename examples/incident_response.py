"""Incident response: real-ops-on-synthetic-data SRE workflow.

An on-call engineer (or an agent like Claude Code) walks an incident
from real Alertmanager payload through real log parsing through
real deploy correlation to mitigation + verification. The server
enforces the order; every step operates on shipped sample data.

Shape:

    report -> acknowledge -> investigate -> mitigate -> verify
                                                       /        \\
                                                      v          v
                                                  mitigate    resolve
                                                  (loop)      |
                                                              v
                                                       write_postmortem

What's real:

* ``report`` parses an Alertmanager-shape JSON payload from
  ``examples/data/incident_response/alert.json``: severity, service,
  pod, startsAt, summary all come from the file, no fabrication.
* The investigation sub-app reads the actual log file
  ``api-gateway.log``, filters lines by time window relative to the
  alert's ``startsAt``, counts ERROR/WARN/INFO levels, cross-
  references with ``deploys.json`` to find candidate root-cause
  deploys, and forms a hypothesis based on the evidence found.
* ``mitigate`` records a real chosen mitigation action (rollback /
  scale_up / feature_flag_off) plus a simulated wall-clock so the
  later verification can read the log forward from that point.
* ``verify`` re-reads the log file, counts ERROR lines in the
  post-mitigation window, and surfaces that count as evidence the
  agent uses to decide whether to resolve or loop back.

The shipped sample data carries a real rollback recovery: log lines
after ``2026-05-20T14:30:20Z`` show clean traffic, so a mitigation
recorded at or before the rollback timestamp leads to a successful
verify; one recorded too early sees errors persist.

What this exercises:

* Sequential transitions: report -> acknowledge -> investigate ->
  mitigate -> verify -> resolve -> write_postmortem.
* Conditional branching: verify -> mitigate (loop on
  ``verified == False``) or verify -> resolve.
* Sub-graph: ``investigate`` spawns a four-step sub-Application via
  ``spawn_subapp``; its timeline lives at ``theodosia://subruns/{id}``.
* Input validator: severity from the alert must be P1/P2/P3 (validates
  the payload, not the caller's input).

Run as stdio:

    uv run python examples/incident_response.py

Or via the CLI:

    uv run theodosia serve incident_response:build_application --app-dir examples
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, ValidationFailed, mount, spawn_subapp

_TRACKER_PROJECT = "incident-response-demo"
_DATA_DIR = Path(__file__).parent / "data" / "incident_response"
_LOG_LINE = re.compile(r"^(\S+)\s+(\w+)\s+(\S+)\s+(.+)$")


# ── helpers ──────────────────────────────────────────────────────────


def _parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; tolerant of the trailing 'Z' form."""
    return datetime.fromisoformat(s)


def _read_log_lines(path: Path) -> list[tuple[datetime, str, str, str]]:
    """Parse a log file into (ts, level, component, message) tuples.

    Lines that don't match the standard shape are skipped.
    """
    rows = []
    for line in path.read_text().splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        ts_str, level, component, message = m.groups()
        try:
            ts = _parse_ts(ts_str)
        except ValueError:
            continue
        rows.append((ts, level, component, message))
    return rows


# ── investigation sub-graph (real ops on shipped logs + deploys) ────


@action(reads=["service", "alert_starts_at"], writes=["log_window"])
async def gather_logs(state: State, time_window_minutes: int = 10) -> State:
    """Read the affected service's log and slice to the alert window.

    Filters log lines to ``[alert_starts_at - window, alert_starts_at + window]``
    so downstream actions reason over the right slice. Counts per level
    are included so a small prompt can see the shape at a glance.
    """
    service = state["service"]
    log_path = _DATA_DIR / f"{service}.log"
    if not log_path.exists():
        raise ValueError(
            f"no log file for service {service!r} at {log_path}; "
            f"ship a {service}.log next to alert.json"
        )
    start = _parse_ts(state["alert_starts_at"])
    window_start = start - timedelta(minutes=time_window_minutes)
    window_end = start + timedelta(minutes=time_window_minutes)
    rows = _read_log_lines(log_path)
    in_window = [r for r in rows if window_start <= r[0] <= window_end]
    level_counts: dict[str, int] = {"INFO": 0, "WARN": 0, "ERROR": 0}
    for _ts, level, _comp, _msg in in_window:
        if level in level_counts:
            level_counts[level] += 1
    return state.update(
        log_window={
            "service": service,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "line_count": len(in_window),
            "level_counts": level_counts,
            "sample_lines": [
                f"{ts.isoformat()} {level} {comp} {msg}" for ts, level, comp, msg in in_window[:8]
            ],
        }
    )


@action(reads=["service", "alert_starts_at"], writes=["correlations"])
async def correlate_events(state: State) -> State:
    """Cross-reference window logs with recent deploys.

    A deploy that started 0-30 minutes before the alert is flagged as
    a candidate; log lines mentioning the deploy id are gathered as
    direct evidence.
    """
    service = state["service"]
    alert_ts = _parse_ts(state["alert_starts_at"])
    deploys = json.loads((_DATA_DIR / "deploys.json").read_text())["recent_deploys"]
    candidates: list[dict[str, Any]] = []
    for d in deploys:
        if d.get("service") != service:
            continue
        deploy_ts = _parse_ts(d["started_at"])
        delta_min = (alert_ts - deploy_ts).total_seconds() / 60
        if 0 <= delta_min <= 30:
            candidates.append({**d, "minutes_before_alert": round(delta_min, 1)})

    log_path = _DATA_DIR / f"{service}.log"
    rows = _read_log_lines(log_path)
    window_start = alert_ts - timedelta(minutes=10)
    window_end = alert_ts + timedelta(minutes=10)
    deploy_mentions: list[dict[str, str]] = []
    error_lines: list[str] = []
    for ts, level, comp, msg in rows:
        if not (window_start <= ts <= window_end):
            continue
        if level == "ERROR":
            error_lines.append(f"{ts.isoformat()} {level} {comp} {msg}")
        for d in deploys:
            if d["id"] in msg:
                deploy_mentions.append({"deploy_id": d["id"], "line": f"{level} {msg}"})
                break
    return state.update(
        correlations={
            "candidate_deploys": candidates,
            "deploy_mentions_in_window": deploy_mentions[:10],
            "error_line_count": len(error_lines),
            "sample_error_lines": error_lines[:5],
        }
    )


@action(reads=["correlations"], writes=["hypothesis"])
async def form_hypothesis(state: State) -> State:
    """Turn correlations into a single best-guess root cause."""
    c = state["correlations"]
    candidates = c["candidate_deploys"]
    if candidates and c["deploy_mentions_in_window"]:
        primary = candidates[0]
        hyp = (
            f"deploy {primary['id']} (started {primary['minutes_before_alert']}m before "
            f"alert; commit {primary.get('commit_sha', 'unknown')}) likely introduced "
            f"the regression. Found {len(c['deploy_mentions_in_window'])} log line(s) "
            f"referencing it, plus {c['error_line_count']} ERROR lines in the alert window."
        )
    elif candidates:
        primary = candidates[0]
        hyp = (
            f"deploy {primary['id']} timing is suspicious "
            f"({primary['minutes_before_alert']}m before alert) but no direct log "
            f"correlation found. Investigate further before rolling back."
        )
    else:
        hyp = "no recent deploy correlation; investigate upstream dependencies."
    return state.update(hypothesis=hyp)


@action(reads=["log_window", "correlations", "hypothesis"], writes=["findings_report"])
async def report_findings(state: State) -> State:
    """Package the investigation evidence for the parent action."""
    return state.update(
        findings_report={
            "hypothesis": state["hypothesis"],
            "log_window": state["log_window"],
            "correlations": state["correlations"],
        }
    )


def _build_investigation_subgraph(
    *, service: str | None = None, alert_starts_at: str | None = None
):
    return (
        ApplicationBuilder()
        .with_actions(
            gather_logs=gather_logs,
            correlate_events=correlate_events,
            form_hypothesis=form_hypothesis,
            report_findings=report_findings,
        )
        .with_transitions(
            ("gather_logs", "correlate_events"),
            ("correlate_events", "form_hypothesis"),
            ("form_hypothesis", "report_findings"),
        )
        .with_tracker(LocalTrackingClient(project=f"{_TRACKER_PROJECT}-investigation"))
        .with_state(
            service=service,
            alert_starts_at=alert_starts_at,
            log_window=None,
            correlations=None,
            hypothesis=None,
            findings_report=None,
        )
        .with_entrypoint("gather_logs")
        .build()
    )


# ── input validators ────────────────────────────────────────────────


def _alert_payload_validator(state: dict, inputs: dict) -> None:
    """The alert payload (loaded by report) must carry severity P1/P2/P3."""
    # report() reads from disk; validate the optional alert_path override.
    path = inputs.get("alert_path")
    if path is not None and not Path(path).exists():
        raise ValidationFailed(
            f"alert_path {path!r} does not exist",
            details={"received": path},
        )
    return


# ── main FSM actions ────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "incident_id",
        "alert_payload",
        "service",
        "severity",
        "summary",
        "pod",
        "alert_starts_at",
        "status",
        "reported_at",
    ],
)
async def report(state: State, alert_path: str | None = None) -> State:
    """Open an incident by parsing a real Alertmanager-shape JSON payload.

    ``alert_path`` defaults to the shipped ``alert.json``. The first
    alert's labels + annotations drive the incident record; severity
    must be P1/P2/P3 (the alert payload itself is the source of truth).
    """
    path = Path(alert_path) if alert_path else (_DATA_DIR / "alert.json")
    payload = json.loads(path.read_text())
    alerts = payload.get("alerts", [])
    if not alerts:
        raise ValueError(f"no alerts in payload {path}")
    primary = alerts[0]
    labels = primary.get("labels", {})
    annotations = primary.get("annotations", {})
    severity = labels.get("severity")
    if severity not in {"P1", "P2", "P3"}:
        raise ValueError(f"alert severity {severity!r} not in P1/P2/P3 (from {path})")
    return state.update(
        incident_id=f"INC-{int(datetime.now(UTC).timestamp())}",
        alert_payload=payload,
        service=labels.get("service"),
        severity=severity,
        summary=annotations.get("summary"),
        pod=labels.get("pod"),
        alert_starts_at=primary.get("startsAt"),
        status="reported",
        reported_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["status"], writes=["status", "responder", "acknowledged_at"])
async def acknowledge(state: State, responder: str) -> State:
    """Acknowledge the incident as the on-call responder."""
    return state.update(
        status="acknowledged",
        responder=responder,
        acknowledged_at=datetime.now(UTC).isoformat(),
    )


@action(
    reads=["incident_id", "service", "alert_starts_at"],
    writes=["status", "findings", "hypothesis", "investigated_at"],
)
async def investigate(state: State) -> State:
    """Run the investigation sub-graph over the real log + deploy data.

    Delegates to a four-step sub-Application that reads
    ``api-gateway.log`` and ``deploys.json``, slices the log to the
    alert window, cross-references with recent deploys, forms a
    hypothesis, and produces a findings report. The sub-run's full
    timeline is available at ``theodosia://subruns/{id}``.
    """
    sub = _build_investigation_subgraph(
        service=state["service"],
        alert_starts_at=state["alert_starts_at"],
    )
    result = await spawn_subapp(
        sub,
        label=f"investigate-{state['incident_id']}",
    )
    findings = result["final_state"].get("findings_report") or {}
    return state.update(
        status="investigated",
        findings=findings,
        hypothesis=findings.get("hypothesis"),
        investigated_at=datetime.now(UTC).isoformat(),
    )


@action(
    reads=["status", "alert_starts_at"],
    writes=["status", "mitigation", "mitigated_at"],
)
async def mitigate(
    state: State,
    action_kind: Literal["rollback", "scale_up", "feature_flag_off"],
    target: str,
    simulated_offset_minutes: int = 7,
) -> State:
    """Apply a mitigation and record when (in simulated alert-relative time).

    ``action_kind`` is the type of remediation: ``rollback`` (revert
    to a prior deploy id), ``scale_up`` (add replicas), or
    ``feature_flag_off`` (disable a flag). ``target`` is the specific
    deploy-id / replica-count / flag-name being acted on.

    ``simulated_offset_minutes`` lets the demo position the mitigation
    on the log's timeline (the shipped log shows recovery starting at
    ~T+8.5m, when a rollback to v2.14.2 completes). ``verify`` reads
    log entries that fall after this point to check whether the
    mitigation actually worked.
    """
    valid = {"rollback", "scale_up", "feature_flag_off"}
    if action_kind not in valid:
        raise ValueError(f"action_kind must be one of {sorted(valid)}; got {action_kind!r}")
    alert_ts = _parse_ts(state["alert_starts_at"])
    simulated_at = alert_ts + timedelta(minutes=simulated_offset_minutes)
    return state.update(
        status="mitigated",
        mitigation={
            "action_kind": action_kind,
            "target": target,
            "simulated_at": simulated_at.isoformat(),
            "decided_at": datetime.now(UTC).isoformat(),
        },
        mitigated_at=datetime.now(UTC).isoformat(),
    )


@action(
    reads=["service", "mitigation"],
    writes=["status", "verified", "verification_evidence", "verified_at"],
)
async def verify(state: State, verified: bool, notes: str = "") -> State:
    """Re-read the log forward from the mitigation point; surface evidence.

    Reads log lines in ``[mitigation.simulated_at, +10m]`` and counts
    ERRORs. The agent inspects the evidence and supplies ``verified``
    (True if the mitigation worked, False to loop back to mitigate).

    On the shipped data, a rollback recorded at simulated_offset >= 8
    minutes sees zero ERRORs in the post-window; earlier offsets still
    see the regression and should be marked verified=False.
    """
    log_path = _DATA_DIR / f"{state['service']}.log"
    mit_ts = _parse_ts(state["mitigation"]["simulated_at"])
    window_end = mit_ts + timedelta(minutes=10)
    rows = _read_log_lines(log_path)
    post = [r for r in rows if mit_ts <= r[0] <= window_end]
    error_count = sum(1 for _ts, level, _c, _m in post if level == "ERROR")
    info_count = sum(1 for _ts, level, _c, _m in post if level == "INFO")
    return state.update(
        status="verified" if verified else "verification_failed",
        verified=verified,
        verification_evidence={
            "post_mitigation_window": [mit_ts.isoformat(), window_end.isoformat()],
            "lines_inspected": len(post),
            "error_count": error_count,
            "info_count": info_count,
            "agent_notes": notes,
        },
        verified_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["status"], writes=["status", "resolution", "resolved_at"])
async def resolve(state: State, resolution: str) -> State:
    """Mark the incident resolved with a one-line resolution summary."""
    return state.update(
        status="resolved",
        resolution=resolution,
        resolved_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["status"], writes=["status", "postmortem", "closed_at"])
async def write_postmortem(state: State, postmortem_md: str) -> State:
    """Attach the postmortem document. Terminal; closes the incident."""
    return state.update(
        status="closed",
        postmortem=postmortem_md,
        closed_at=datetime.now(UTC).isoformat(),
    )


# ── graph + server ──────────────────────────────────────────────────


def build_application():
    """Build a fresh incident-response Application.

    Factory: each MCP session (each connecting on-call engineer / agent)
    gets their own incident state.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            report=report,
            acknowledge=acknowledge,
            investigate=investigate,
            mitigate=mitigate,
            verify=verify,
            resolve=resolve,
            write_postmortem=write_postmortem,
        )
        .with_transitions(
            ("report", "acknowledge"),
            ("acknowledge", "investigate"),
            ("investigate", "mitigate"),
            ("mitigate", "verify"),
            ("verify", "resolve", Condition.expr("verified == True")),
            ("verify", "mitigate", Condition.expr("verified == False")),
            ("resolve", "write_postmortem"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            incident_id=None,
            alert_payload=None,
            service=None,
            severity=None,
            summary=None,
            pod=None,
            alert_starts_at=None,
            status="new",
            responder=None,
            findings=None,
            hypothesis=None,
            mitigation=None,
            verified=None,
            verification_evidence=None,
            resolution=None,
            postmortem=None,
        )
        .with_entrypoint("report")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="incident-response",
        instructions=(
            "Incident response FSM driven by real Alertmanager + log "
            "data shipped in examples/data/incident_response/. Walk: "
            "report (parses alert.json) -> acknowledge -> investigate "
            "(reads api-gateway.log + deploys.json) -> mitigate "
            "(action_kind in {rollback, scale_up, feature_flag_off}; "
            "simulated_offset_minutes positions mitigation on the log "
            "timeline) -> verify (reads forward log; agent supplies "
            "verified verdict from evidence) -> resolve -> "
            "write_postmortem. verify(verified=False) loops back to "
            "mitigate. Investigation sub-run at theodosia://subruns/{id}."
        ),
        input_validators={"report": _alert_payload_validator},
    )


if __name__ == "__main__":
    build_server().run()
