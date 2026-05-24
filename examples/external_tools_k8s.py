"""external_tools: a Burr graph as a cross-MCP-server playbook.

This FSM holds NO tools of its own for touching Kubernetes. Instead each
action declares, via ``mount(external_tools=...)``, which tools on a
SEPARATE Kubernetes MCP server are relevant when that action is the next
move. The driving agent is connected to both servers; it reads
``next_external_tools`` off each step response, calls those tools on the
k8s server, then ``step()``s here to record what it found and advance.

The Burr graph is the conductor; the k8s MCP server is the orchestra.
BurrMCP sequences the external tools (which to use, in what phase); it
never executes them.

Shape:

    triage  --observe-->  diagnose  --decide-->  remediate  --confirm-->  resolved

Per-phase external k8s tools (names match a typical kubernetes MCP):

    triage:    list_pods, list_events, get_pod_logs
    diagnose:  describe_pod, top_pods, get_pod_logs
    remediate: rollout_restart, scale_deployment, cordon_node
    confirm:   list_pods, get_deployment

Run:

    burrmcp serve external_tools_k8s:build_application --name k8s-incident
    # then connect an agent to BOTH this server and a kubernetes MCP server.
"""

from __future__ import annotations

from typing import Any

from burr.core import ApplicationBuilder, Condition, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "k8s-incident-demo"


@action(reads=[], writes=["incident", "phase", "observations", "decision", "log"])
async def start_incident(state: State, incident: str) -> State:
    """Open a Kubernetes incident investigation.

    Args:
        incident: the alert / symptom, e.g. "checkout-api pods CrashLooping".
    """
    if not incident.strip():
        raise ValueError("incident must not be empty")
    return state.update(
        incident=incident.strip(),
        phase="triage",
        observations=[],
        decision=None,
        log=[f"incident opened: {incident.strip()}"],
    )


@action(reads=["observations", "log"], writes=["observations", "phase", "log"])
async def record_observation(state: State, finding: str, source_tool: str) -> State:
    """Record one observation gathered from the k8s MCP server.

    Args:
        finding: what you observed (e.g. "3/3 checkout-api pods in CrashLoopBackOff").
        source_tool: which external k8s tool produced it (e.g. "list_pods").
    """
    if not finding.strip():
        raise ValueError("finding must not be empty")
    obs = [*state["observations"], {"finding": finding.strip(), "source_tool": source_tool}]
    return state.update(
        observations=obs,
        phase="diagnose" if len(obs) >= 2 else "triage",
        log=[*state["log"], f"observed via {source_tool}: {finding.strip()[:60]}"],
    )


@action(reads=["observations", "phase", "log"], writes=["decision", "phase", "log"])
async def decide_remediation(state: State, decision: str, rationale: str) -> State:
    """Commit to a remediation plan. Requires >=2 observations first.

    Args:
        decision: the action to take (e.g. "rollout_restart checkout-api").
        rationale: why, grounded in the observations.
    """
    if len(state["observations"]) < 2:
        raise ValueError(
            "decide_remediation requires at least 2 observations; "
            "gather more evidence from the k8s server first."
        )
    if not decision.strip() or not rationale.strip():
        raise ValueError("decision and rationale must both be non-empty")
    return state.update(
        decision={"decision": decision.strip(), "rationale": rationale.strip()},
        phase="remediate",
        log=[*state["log"], f"decided: {decision.strip()[:60]}"],
    )


@action(reads=["decision", "log"], writes=["phase", "resolution", "log"])
async def confirm_resolution(state: State, confirmed: bool, evidence: str) -> State:
    """Terminal. Confirm the remediation worked, grounded in a fresh k8s read.

    Args:
        confirmed: whether the incident is resolved.
        evidence: the post-remediation observation supporting it.
    """
    if not evidence.strip():
        raise ValueError("evidence must not be empty")
    return state.update(
        phase="resolved" if confirmed else "remediate",
        resolution={"confirmed": bool(confirmed), "evidence": evidence.strip()},
        log=[*state["log"], f"resolution confirmed={confirmed}"],
    )


# Per-action external tools: which kubernetes-MCP tools are relevant when
# each action is the next move. The agent reads these off next_external_tools.
EXTERNAL_TOOLS = {
    "start_incident": ["list_pods", "list_events"],
    "record_observation": ["list_pods", "list_events", "get_pod_logs", "describe_pod", "top_pods"],
    "decide_remediation": ["rollout_restart", "scale_deployment", "cordon_node"],
    "confirm_resolution": ["list_pods", "get_deployment"],
}

_OPEN = Condition.expr("phase != 'resolved'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_incident=start_incident,
            record_observation=record_observation,
            decide_remediation=decide_remediation,
            confirm_resolution=confirm_resolution,
        )
        .with_transitions(
            ("start_incident", "record_observation", _OPEN),
            ("record_observation", "record_observation", _OPEN),
            ("record_observation", "decide_remediation", _OPEN),
            ("decide_remediation", "confirm_resolution", _OPEN),
            ("confirm_resolution", "record_observation", _OPEN),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            incident="",
            phase="new",
            observations=[],
            decision=None,
            resolution=None,
            log=[],
        )
        .with_entrypoint("start_incident")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="k8s-incident",
        external_tools=EXTERNAL_TOOLS,
        instructions=(
            "A Kubernetes incident-investigation FSM that orchestrates a "
            "SEPARATE kubernetes MCP server. This server holds no k8s tools; "
            "each step response carries next_external_tools telling you which "
            "tools on the connected kubernetes MCP server to call for the "
            "reachable actions. Walk: start_incident(incident) -> "
            "record_observation(finding, source_tool) [>=2 before deciding] -> "
            "decide_remediation(decision, rationale) -> "
            "confirm_resolution(confirmed, evidence). For each step, call the "
            "external k8s tools named in next_external_tools, then step() here "
            "to record what you found."
        ),
    )


if __name__ == "__main__":
    build_server().run()
