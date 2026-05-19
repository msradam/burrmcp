"""Incident response workflow: the canonical burr-mcp sample.

A small but realistic ops workflow. An on-call engineer (or an
agent like Claude Code) walks an incident from report to postmortem,
with the server enforcing the order and recording every step.

Shape:

    report -> acknowledge -> investigate -> mitigate -> verify
                                                       /        \\
                                                      v          v
                                                  mitigate    resolve
                                                  (loop)      |
                                                              v
                                                       write_postmortem

What each piece of the library this exercises:

  • Sequential transitions: report -> acknowledge -> investigate -> mitigate -> verify.
  • Conditional branching: ``verify`` reads ``verified``; transitions
    to ``mitigate`` again if False, to ``resolve`` if True.
  • Sub-graph: ``investigate`` spawns a small four-step investigation
    sub-Application via ``spawn_subapp``. Its timeline is addressable
    via ``burr://subruns/{id}``.
  • Input validator: severity must be one of P1/P2/P3.
  • Per-action timeout via ``ToolSpec`` isn't used here (no flat-MCP
    import), but you'd add one with ``fn._burr_mcp_timeout_seconds = N``.
  • Per-session isolation via factory (each on-call engineer gets
    their own incident, even on a shared server).

Run as a stdio server (the shape Claude Code expects):

    uv run python examples/incident_response.py

Or via the CLI:

    uv run burr-mcp serve incident_response:build_application --app-dir examples

See ``examples/claude-code.example.json`` for how to wire this into
Claude Code's MCP config.
"""

from __future__ import annotations

from datetime import UTC, datetime

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from burr_mcp import (
    ServingMode,
    ValidationFailed,
    mount,
    spawn_subapp,
)

# ── investigation sub-graph ──────────────────────────────────────────


@action(reads=["incident_id"], writes=["logs"])
async def gather_logs(state: State, time_window_minutes: int = 15) -> State:
    """Pull recent logs for the affected service.

    In a real system this would call out to your logs API. Here we
    return a structured stub so the sub-graph can be walked end-to-end
    without external services.
    """
    return state.update(
        logs={
            "incident_id": state.get("incident_id"),
            "time_window_minutes": time_window_minutes,
            "lines": [
                "ERROR api.handler: timeout on upstream call",
                "ERROR api.handler: timeout on upstream call",
                "WARN  api.deploy: rollout 89a3 started",
            ],
        }
    )


@action(reads=["logs"], writes=["correlations"])
async def correlate_events(state: State) -> State:
    """Match log events against recent deploys and config changes."""
    logs = state.get("logs", {})
    return state.update(
        correlations={
            "matched_deploy": "89a3"
            if any("89a3" in line for line in logs.get("lines", []))
            else None,
            "matching_lines": [line for line in logs.get("lines", []) if "ERROR" in line],
        }
    )


@action(reads=["correlations"], writes=["hypothesis"])
async def form_hypothesis(state: State) -> State:
    """Turn the correlations into a single best-guess root cause."""
    c = state.get("correlations", {}) or {}
    deploy = c.get("matched_deploy")
    if deploy:
        hyp = f"deploy {deploy} introduced regression; symptoms match its rollout window"
    else:
        hyp = "no deploy correlation found; investigate dependencies"
    return state.update(hypothesis=hyp)


@action(reads=["hypothesis", "correlations", "logs"], writes=["findings_report"])
async def report_findings(state: State) -> State:
    """Produce a short findings report the parent action consumes."""
    return state.update(
        findings_report={
            "hypothesis": state.get("hypothesis"),
            "evidence": state.get("correlations"),
            "log_sample": (state.get("logs") or {}).get("lines", [])[:3],
        }
    )


def _build_investigation_subgraph():
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
        .with_state(
            incident_id=None,
            logs=None,
            correlations=None,
            hypothesis=None,
            findings_report=None,
        )
        .with_entrypoint("gather_logs")
        .build()
    )


# ── input validators ────────────────────────────────────────────────


def _severity_validator(state: dict, inputs: dict) -> None:
    sev = inputs.get("severity")
    if sev not in {"P1", "P2", "P3"}:
        raise ValidationFailed(
            "severity must be one of P1, P2, P3",
            details={"received": sev, "allowed": ["P1", "P2", "P3"]},
        )
    return None


# ── main FSM actions ────────────────────────────────────────────────


@action(
    reads=[], writes=["incident_id", "summary", "severity", "reporter", "status", "reported_at"]
)
async def report(state: State, summary: str, severity: str, reporter: str) -> State:
    """Open a new incident.

    ``severity`` must be one of P1, P2, P3 (the validator enforces this
    before the action runs). The server assigns an ``incident_id``.
    """
    return state.update(
        incident_id=f"INC-{int(datetime.now(UTC).timestamp())}",
        summary=summary,
        severity=severity,
        reporter=reporter,
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
    reads=["incident_id", "status"],
    writes=["status", "findings", "hypothesis", "investigated_at"],
)
async def investigate(state: State, hypothesis_seed: str = "") -> State:
    """Run the investigation sub-graph and fold its findings back.

    Delegates to a four-step sub-Application that gathers logs,
    correlates events, forms a hypothesis, and produces a findings
    report. The sub-run's full timeline is available at
    ``burr://subruns/{id}``; the parent history entry for
    ``investigate`` references the sub-run id under ``subruns``.
    """
    sub = _build_investigation_subgraph()
    result = await spawn_subapp(
        sub,
        label=f"investigate-{state.get('incident_id')}",
        inputs={"incident_id": state.get("incident_id")},
    )
    findings = result["final_state"].get("findings_report") or {}
    hypothesis = findings.get("hypothesis") or hypothesis_seed
    return state.update(
        status="investigated",
        findings=findings,
        hypothesis=hypothesis,
        investigated_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["status"], writes=["status", "mitigation", "mitigated_at"])
async def mitigate(state: State, mitigation: str) -> State:
    """Apply a mitigation. The graph allows looping back here from
    ``verify`` if the verification doesn't pass."""
    return state.update(
        status="mitigated",
        mitigation=mitigation,
        mitigated_at=datetime.now(UTC).isoformat(),
    )


@action(reads=["mitigation"], writes=["status", "verified", "verification_notes", "verified_at"])
async def verify(state: State, verified: bool, notes: str = "") -> State:
    """Verify whether the mitigation resolved the symptoms.

    Sets ``verified`` in state. The outgoing transition is gated on
    that value: True moves to ``resolve``; False loops back to
    ``mitigate`` so the responder can try again.
    """
    return state.update(
        status="verified" if verified else "verification_failed",
        verified=verified,
        verification_notes=notes,
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

    Used as a factory so each MCP session (each connecting on-call
    engineer / agent) gets their own incident state.
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
            # Branch on the verification result.
            ("verify", "resolve", Condition.expr("verified == True")),
            ("verify", "mitigate", Condition.expr("verified == False")),
            ("resolve", "write_postmortem"),
        )
        .with_state(
            incident_id=None,
            status="new",
            severity=None,
            summary=None,
            reporter=None,
            responder=None,
            findings=None,
            hypothesis=None,
            mitigation=None,
            verified=None,
            verification_notes=None,
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
            "Incident response FSM. Walk an incident from report to "
            "postmortem in order: report -> acknowledge -> investigate "
            "-> mitigate -> verify -> resolve -> write_postmortem. "
            "verify(verified=False) loops back to mitigate. "
            "Read burr://state for the current incident, "
            "burr://next for the legal next action, "
            "burr://history for the audit trail of this session, "
            "burr://subruns to find the investigation sub-run, and "
            "burr://subruns/{id} for the sub-run's step-by-step timeline."
        ),
        input_validators={"report": _severity_validator},
        # An action_timeout_seconds=N would cap every action; left
        # off here so investigation sub-runs in the demo aren't time-
        # pressured.
    )


if __name__ == "__main__":
    build_server().run()
