"""Reusable graph fragments via Burr's GraphBuilder + with_graph().

Burr lets you build a ``Graph`` object independently of any
``Application``, then embed it into one or more applications via
``ApplicationBuilder.with_graph(...)``. The fragment can carry its
own actions and internal transitions; the parent connects its own
actions to the fragment's entry / exit points by name.

This demo defines a small two-action ``approval`` subgraph
(``submit_for_review`` -> ``decide_review``) and uses the same Graph
object inside TWO different parent applications:

* ``loan_application``: intake -> submit_for_review -> decide_review
  -> finalize_loan
* ``deployment_pipeline``: stage_deploy -> submit_for_review ->
  decide_review -> complete_deploy

Both parents reference the exact same Graph instance. This validates
that graph-level reuse passes through ``mount()`` unchanged.

Run (defaults to the loan parent; pass ``DEMO=deployment`` for the
other parent):

    uv run python examples/subgraph_composition.py

Inspect ``theodosia://graph`` and you'll see the full composed graph:
the parent's actions and the embedded fragment's actions live in
the same namespace, and transitions wire them together.
"""

from __future__ import annotations

import os

from burr.core import ApplicationBuilder, State, action
from burr.core.graph import Graph, GraphBuilder
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "subgraph-composition-demo"


# == the reusable approval subgraph ==================================


@action(reads=["payload"], writes=["review_id", "review_stage"])
def submit_for_review(state: State, reviewer: str) -> State:
    """Submit the current payload to a named reviewer."""
    review_id = f"REV-{abs(hash((state['payload'], reviewer))) % 100000:05d}"
    return state.update(review_id=review_id, review_stage="submitted")


@action(reads=["review_id", "review_stage"], writes=["review_stage", "decision"])
def decide_review(state: State, decision: str, notes: str = "") -> State:
    """Capture the reviewer's decision. Must be ``approve`` or ``reject``."""
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be 'approve' or 'reject'; got {decision!r}")
    return state.update(review_stage="decided", decision=decision)


def build_approval_subgraph() -> Graph:
    """Build the reusable approval ``Graph``.

    Crucially this returns a plain ``Graph`` object, not an
    ``Application``. Multiple parent Applications can embed the same
    Graph via ``with_graph(...)`` and connect to its action names
    (``submit_for_review`` / ``decide_review``) by transition.
    """
    return (
        GraphBuilder()
        .with_actions(
            submit_for_review=submit_for_review,
            decide_review=decide_review,
        )
        .with_transitions(("submit_for_review", "decide_review"))
        .build()
    )


# == loan-application parent =========================================


@action(reads=[], writes=["applicant", "loan_amount", "payload"])
def intake(state: State, applicant: str, loan_amount: float) -> State:
    """Collect the loan applicant + amount. Hands off to the
    approval subgraph by writing ``payload`` (which the subgraph
    reads)."""
    if loan_amount <= 0:
        raise ValueError(f"loan_amount must be positive; got {loan_amount}")
    return state.update(
        applicant=applicant,
        loan_amount=loan_amount,
        payload=f"loan:{applicant}:{loan_amount}",
    )


@action(reads=["decision", "applicant"], writes=["final_status"])
def finalize_loan(state: State) -> State:
    """Terminal for the loan parent. Maps the subgraph's decision to a
    domain-specific final status."""
    status = "loan_approved" if state["decision"] == "approve" else "loan_denied"
    return state.update(final_status=status)


def build_loan_application():
    return (
        ApplicationBuilder()
        .with_actions(intake=intake, finalize_loan=finalize_loan)
        .with_transitions(
            ("intake", "submit_for_review"),
            ("decide_review", "finalize_loan"),
        )
        .with_graph(build_approval_subgraph())
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            applicant=None,
            loan_amount=0.0,
            payload=None,
            review_id=None,
            review_stage=None,
            decision=None,
            final_status=None,
        )
        .with_entrypoint("intake")
        .build()
    )


# == deployment-pipeline parent ======================================


@action(reads=[], writes=["service", "version", "payload"])
def stage_deploy(state: State, service: str, version: str) -> State:
    """Stage a service/version for deployment. Hands off to the
    approval subgraph."""
    return state.update(
        service=service,
        version=version,
        payload=f"deploy:{service}@{version}",
    )


@action(reads=["decision", "service", "version"], writes=["final_status"])
def complete_deploy(state: State) -> State:
    """Terminal for the deployment parent."""
    status = "deploy_promoted" if state["decision"] == "approve" else "deploy_rejected"
    return state.update(final_status=status)


def build_deployment_application():
    return (
        ApplicationBuilder()
        .with_actions(stage_deploy=stage_deploy, complete_deploy=complete_deploy)
        .with_transitions(
            ("stage_deploy", "submit_for_review"),
            ("decide_review", "complete_deploy"),
        )
        .with_graph(build_approval_subgraph())
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            service=None,
            version=None,
            payload=None,
            review_id=None,
            review_stage=None,
            decision=None,
            final_status=None,
        )
        .with_entrypoint("stage_deploy")
        .build()
    )


# == build_server: pick which parent based on env ====================


def build_application():
    """Default entry point; controlled by ``DEMO`` env var."""
    if os.environ.get("DEMO", "loan").lower() == "deployment":
        return build_deployment_application()
    return build_loan_application()


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="subgraph-composition",
        instructions=(
            "Two parent FSMs (loan-application and deployment-pipeline) "
            "embed the same reusable approval Graph (submit_for_review "
            "-> decide_review). Default parent is the loan one; set "
            "DEMO=deployment in the server's env to switch. Walk: "
            "[intake | stage_deploy] -> submit_for_review(reviewer) -> "
            "decide_review(decision={'approve'|'reject'}, notes='...') "
            "-> [finalize_loan | complete_deploy]. Read theodosia://graph "
            "for the composed action list."
        ),
    )


if __name__ == "__main__":
    build_server().run()
