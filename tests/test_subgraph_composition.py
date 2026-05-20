"""Tests for examples/subgraph_composition.py.

Validates that the same Graph object can be embedded in two
different parent Applications via with_graph(), that the parent's
transitions correctly wire to the subgraph's named actions, and
that both parents walk through the embedded subgraph cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from subgraph_composition import (  # noqa: E402
    build_approval_subgraph,
    build_deployment_application,
    build_loan_application,
)


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def test_subgraph_returns_graph_with_expected_actions():
    """The reusable Graph carries both subgraph actions and the
    internal transition."""
    graph = build_approval_subgraph()
    names = {a.name for a in graph.actions}
    assert names == {"submit_for_review", "decide_review"}


def test_loan_parent_embeds_subgraph_actions():
    app = build_loan_application()
    names = {a.name for a in app.graph.actions}
    # Parent's own actions + subgraph's actions.
    assert names == {
        "intake",
        "finalize_loan",
        "submit_for_review",
        "decide_review",
    }


def test_deployment_parent_embeds_same_subgraph_actions():
    app = build_deployment_application()
    names = {a.name for a in app.graph.actions}
    assert names == {
        "stage_deploy",
        "complete_deploy",
        "submit_for_review",
        "decide_review",
    }


def test_loan_full_walk_approve():
    app = build_loan_application()
    _force_step(app, "intake", applicant="alice", loan_amount=10000.0)
    _force_step(app, "submit_for_review", reviewer="bob")
    _force_step(app, "decide_review", decision="approve")
    _force_step(app, "finalize_loan")
    assert app.state["final_status"] == "loan_approved"
    assert app.state["decision"] == "approve"
    assert app.state["review_id"] is not None


def test_loan_full_walk_reject():
    app = build_loan_application()
    _force_step(app, "intake", applicant="alice", loan_amount=10000.0)
    _force_step(app, "submit_for_review", reviewer="bob")
    _force_step(app, "decide_review", decision="reject", notes="risk too high")
    _force_step(app, "finalize_loan")
    assert app.state["final_status"] == "loan_denied"


def test_deployment_full_walk():
    app = build_deployment_application()
    _force_step(app, "stage_deploy", service="api", version="1.2.3")
    _force_step(app, "submit_for_review", reviewer="ops")
    _force_step(app, "decide_review", decision="approve")
    _force_step(app, "complete_deploy")
    assert app.state["final_status"] == "deploy_promoted"


def test_intake_rejects_non_positive_loan_amount():
    app = build_loan_application()
    with pytest.raises(ValueError, match="loan_amount"):
        _force_step(app, "intake", applicant="alice", loan_amount=0.0)


def test_decide_review_rejects_invalid_decision():
    """The subgraph's action-body validation fires regardless of
    which parent embeds it."""
    app = build_loan_application()
    _force_step(app, "intake", applicant="alice", loan_amount=10000.0)
    _force_step(app, "submit_for_review", reviewer="bob")
    with pytest.raises(ValueError, match="decision must be"):
        _force_step(app, "decide_review", decision="maybe")


@pytest.mark.asyncio
async def test_mcp_walk_loan_parent():
    """End-to-end via MCP: same subgraph actions are addressable as
    `step(action="submit_for_review", ...)` etc."""
    server = mount(
        build_loan_application,
        mode=ServingMode.STEP,
        name="subgraph-loan-test",
    )
    async with Client(server) as client:
        for action_name, inputs in [
            ("intake", {"applicant": "alice", "loan_amount": 25000.0}),
            ("submit_for_review", {"reviewer": "underwriter"}),
            ("decide_review", {"decision": "approve"}),
            ("finalize_loan", {}),
        ]:
            r = await client.call_tool("step", {"action": action_name, "inputs": inputs})
            out = json.loads(r.content[0].text)
            assert out.get("error") is None, f"{action_name}: {out}"
        assert out["state"]["final_status"] == "loan_approved"
