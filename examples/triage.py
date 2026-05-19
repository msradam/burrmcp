"""Triage FSM with branching transitions.

Demonstrates that the gating logic handles conditional transitions,
not just linear graphs.

Shape:

    intake -> classify -> (urgent | routine | spam)
    urgent  -> escalate
    routine -> queue
    spam    -> drop

``classify`` writes ``severity`` into state. The transitions out of
``classify`` are gated on that value, so ``burr://next`` returns a
single-element list after ``classify`` runs, but a client trying to
call the wrong branch gets ``invalid_transition``.

Run:

    python examples/triage.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount

_TRACKER_PROJECT = "triage-demo"


@action(reads=[], writes=["stage", "subject", "body"])
def intake(state: State, subject: str, body: str) -> State:
    """Record a new ticket. Subject and body are arbitrary text."""
    return state.update(stage="intake", subject=subject, body=body)


@action(reads=["subject", "body"], writes=["stage", "severity"])
def classify(state: State, severity: str) -> State:
    """Classify the ticket. Severity must be one of urgent, routine, spam.

    In a real system this might call an LLM or a rules engine; here
    the client supplies the label so the example stays self-contained.
    """
    if severity not in {"urgent", "routine", "spam"}:
        # Burr lets actions raise; the action wrapper surfaces it to
        # the caller, in our case as a tool error the LLM can read.
        raise ValueError(f"severity must be one of urgent, routine, spam (got {severity!r})")
    return state.update(stage="classified", severity=severity)


@action(reads=["severity"], writes=["stage", "ticket_id"])
def escalate(state: State, oncall: str) -> State:
    """Page the oncall engineer. Only reachable when severity == urgent."""
    return state.update(stage="escalated", ticket_id=f"INC-{oncall.upper()}-001")


@action(reads=["severity"], writes=["stage", "queue_position"])
def queue(state: State) -> State:
    """Enqueue for normal handling. Only reachable when severity == routine."""
    return state.update(stage="queued", queue_position=42)


@action(reads=["severity"], writes=["stage"])
def drop(state: State) -> State:
    """Drop the ticket. Only reachable when severity == spam."""
    return state.update(stage="dropped")


def build_application():
    """Build the triage Application.

    Transitions out of ``classify`` are gated by ``severity``. Burr
    evaluates the conditions against current state to pick the next
    action; burr-mcp surfaces those same conditions to clients via
    ``burr://next``.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            intake=intake,
            classify=classify,
            escalate=escalate,
            queue=queue,
            drop=drop,
        )
        .with_transitions(
            ("intake", "classify"),
            ("classify", "escalate", Condition.expr("severity == 'urgent'")),
            ("classify", "queue", Condition.expr("severity == 'routine'")),
            ("classify", "drop", Condition.expr("severity == 'spam'")),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(stage="new")
        .with_entrypoint("intake")
        .build()
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the triage Application as an MCP server.

    Uses a factory so each MCP session gets its own ticket. Two clients
    connected to the same server work independent tickets.
    """
    return mount(
        build_application,
        mode=mode,
        name="triage",
        instructions=(
            "Ticket triage FSM. Call intake with subject+body, then "
            "classify with severity (urgent/routine/spam). The next "
            "valid action depends on classification: urgent routes to "
            "escalate(oncall), routine to queue, spam to drop. Read "
            "burr://state and burr://next at any time."
        ),
    )


if __name__ == "__main__":
    build_server().run()
