"""Tests for examples/differential_review.py.

Mirrors test_security_audit.py's shape (action-level unit tests
+ FSM walks via _force_step + MCP wire roundtrip). The load-bearing
behaviours:

* Action input validation (target empty, invalid codebase_size,
  pre_analysis missing changed_files, invalid risk labels).
* overall_risk aggregation (HIGH wins over MEDIUM wins over LOW).
* Branching at blast_radius: HIGH -> deep_context; otherwise ->
  write_report skips phases 4-5.
* write_report builds a per-severity summary across the prior phases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from burrmcp import ServingMode, mount  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from differential_review import (  # noqa: E402
    blast_radius,
    build_application,
    build_server,
    pre_analysis,
    start_review,
    triage,
    write_report,
)
from differential_review import test_coverage as _test_coverage_action  # noqa: E402,F401

# == action unit tests ============================================


def _initial_state(**overrides):
    from burr.core.state import State

    base = {
        "target": "",
        "codebase_size": "",
        "scope": "",
        "baseline": {},
        "per_file_risk": {},
        "overall_risk": "UNKNOWN",
        "code_findings": [],
        "coverage": {},
        "blast": {},
        "deep_context": {},
        "adversarial_scenarios": [],
        "report": None,
        "report_summary": None,
        "current_prompt": "",
        "log": [],
    }
    base.update(overrides)
    return State(base)


def test_start_review_rejects_empty_target():
    with pytest.raises(ValueError):
        start_review(_initial_state(), target="   ", codebase_size="SMALL")


def test_start_review_rejects_invalid_codebase_size():
    with pytest.raises(ValueError):
        start_review(_initial_state(), target="PR-1", codebase_size="HUGE")


def test_start_review_stamps_target_and_initialises_state():
    out = start_review(
        _initial_state(),
        target="PR-7",
        codebase_size="MEDIUM",
        scope="release-v3 cut",
    )
    assert out["target"] == "PR-7"
    assert out["codebase_size"] == "MEDIUM"
    assert out["scope"] == "release-v3 cut"
    assert out["overall_risk"] == "UNKNOWN"
    assert "PR-7" in out["current_prompt"]


def test_pre_analysis_rejects_empty_changed_files():
    s = _initial_state(target="PR-1", codebase_size="SMALL", log=["Review started"])
    with pytest.raises(ValueError, match="changed_files"):
        pre_analysis(s, baseline={"changed_files": []})


def test_triage_rejects_invalid_label():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError, match="HIGH"):
        triage(s, per_file_risk={"src/a.py": "EXTREME"})


def test_triage_rejects_empty_dict():
    s = _initial_state(log=["..."])
    with pytest.raises(ValueError, match="at least one"):
        triage(s, per_file_risk={})


@pytest.mark.parametrize(
    "labels,expected",
    [
        ({"a.py": "HIGH", "b.py": "LOW"}, "HIGH"),
        ({"a.py": "MEDIUM", "b.py": "LOW"}, "MEDIUM"),
        ({"a.py": "LOW", "b.py": "LOW"}, "LOW"),
        ({"a.py": "MEDIUM", "b.py": "HIGH", "c.py": "MEDIUM"}, "HIGH"),
    ],
)
def test_triage_aggregates_overall_risk_correctly(labels, expected):
    s = _initial_state(log=["..."])
    out = triage(s, per_file_risk=labels)
    assert out["overall_risk"] == expected


def test_blast_radius_emits_deep_context_prompt_when_high():
    s = _initial_state(overall_risk="HIGH", target="PR-1", codebase_size="SMALL", log=["..."])
    out = blast_radius(s, blast={"per_file_callers": {}})
    assert "PHASE 4: DEEP CONTEXT" in out["current_prompt"]


def test_blast_radius_emits_report_prompt_when_not_high():
    s = _initial_state(overall_risk="MEDIUM", target="PR-1", codebase_size="SMALL", log=["..."])
    out = blast_radius(s, blast={"per_file_callers": {}})
    assert "PHASE 6: REPORT GENERATION" in out["current_prompt"]
    assert "skipped" in out["current_prompt"]


def test_write_report_rejects_empty_report():
    s = _initial_state(
        target="PR-1",
        overall_risk="HIGH",
        code_findings=[],
        coverage={},
        blast={},
        deep_context={},
        adversarial_scenarios=[],
        log=["..."],
    )
    with pytest.raises(ValueError):
        write_report(s, report="   ")


def test_write_report_builds_severity_summary():
    s = _initial_state(
        target="PR-1",
        overall_risk="HIGH",
        code_findings=[
            {"severity": "high"},
            {"severity": "medium"},
            {"severity": "high"},
            {"severity": "info"},
        ],
        coverage={},
        blast={"per_file_callers": {"a": {}, "b": {}}},
        deep_context={"per_file_context": {"a": {}}},
        adversarial_scenarios=[{"file": "a"}],
        log=["..."],
    )
    out = write_report(s, report="# Report\n\nbody")
    summary = out["report_summary"]
    assert summary["total_findings"] == 4
    assert summary["findings_by_severity"] == {"high": 2, "medium": 1, "info": 1}
    assert summary["blast_radius_files"] == 2
    assert summary["adversarial_scenarios"] == 1


# == FSM walks ====================================================


def _force_step(app, action_name: str, **inputs):
    target = app.graph.get_action(action_name)
    original = app.get_next_action
    app.get_next_action = lambda: target
    try:
        app.step(inputs=inputs or None)
    finally:
        app.get_next_action = original


def _walk_through_blast_radius(app, overall_risk_target: str):
    """Walk the common path up to and including blast_radius.

    `overall_risk_target` controls the per_file_risk labels so the
    aggregated overall_risk matches what the test wants to exercise.
    """
    _force_step(app, "start_review", target="PR-99", codebase_size="SMALL")
    _force_step(
        app,
        "pre_analysis",
        baseline={
            "changed_files": ["src/a.py"],
            "removed_security_code": [],
            "entrypoints_touched": [],
            "dependencies_touched": [],
        },
    )
    _force_step(app, "triage", per_file_risk={"src/a.py": overall_risk_target})
    _force_step(app, "code_analysis", findings=[])
    _force_step(app, "test_coverage", coverage={})
    _force_step(app, "blast_radius", blast={"per_file_callers": {}})


def test_full_walk_high_risk_passes_through_all_seven_phases():
    app = build_application()
    _walk_through_blast_radius(app, "HIGH")
    _force_step(app, "deep_context", context={})
    _force_step(app, "adversarial", scenarios=[])
    _force_step(app, "write_report", report="# done")
    assert app.state["overall_risk"] == "HIGH"
    assert app.state["report_summary"] is not None
    assert app.state["report"] == "# done"


def test_full_walk_low_risk_skips_phases_4_and_5():
    app = build_application()
    _walk_through_blast_radius(app, "LOW")
    # In the LOW path, blast_radius emits the report prompt directly;
    # deep_context is not a valid next move.
    _force_step(app, "write_report", report="# done LOW")
    assert app.state["overall_risk"] == "LOW"
    assert app.state["report"] == "# done LOW"
    assert app.state["deep_context"] == {}
    assert app.state["adversarial_scenarios"] == []


# == MCP wire roundtrip ==========================================


@pytest.mark.asyncio
async def test_mcp_step_through_high_risk_review():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "start_review",
                "inputs": {"target": "PR-100", "codebase_size": "SMALL"},
            },
        )
        out = json.loads(r.content[0].text)
        assert out["action"] == "start_review"

        r = await client.call_tool(
            "step",
            {
                "action": "pre_analysis",
                "inputs": {
                    "baseline": {
                        "changed_files": ["src/auth.py"],
                        "removed_security_code": [],
                        "entrypoints_touched": [],
                        "dependencies_touched": [],
                    }
                },
            },
        )
        assert "PHASE 0" in json.loads(r.content[0].text)["state"]["current_prompt"]

        r = await client.call_tool(
            "step",
            {
                "action": "triage",
                "inputs": {"per_file_risk": {"src/auth.py": "HIGH"}},
            },
        )
        out = json.loads(r.content[0].text)
        assert out["state"]["overall_risk"] == "HIGH"
        assert (
            "deep_context" in out["valid_next_actions"]
            or "code_analysis" in out["valid_next_actions"]
        )


@pytest.mark.asyncio
async def test_mcp_step_refuses_skipping_pre_analysis():
    """Cannot call triage before pre_analysis: the FSM refuses."""
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "start_review",
                "inputs": {"target": "PR-101", "codebase_size": "SMALL"},
            },
        )
        # The valid_next_actions after start_review should be only
        # pre_analysis; triage should refuse with invalid_transition.
        r = await client.call_tool(
            "step",
            {
                "action": "triage",
                "inputs": {"per_file_risk": {"src/a.py": "HIGH"}},
            },
        )
        out = json.loads(r.content[0].text)
        assert out.get("error") == "invalid_transition"
        assert "pre_analysis" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_mcp_step_refuses_skipping_adversarial_on_high_risk():
    """After blast_radius on a HIGH-risk review, write_report should
    refuse: phases 4 and 5 are required."""
    server = build_server()
    async with Client(server) as client:
        for action_name, inputs in [
            ("start_review", {"target": "PR-102", "codebase_size": "SMALL"}),
            (
                "pre_analysis",
                {
                    "baseline": {
                        "changed_files": ["src/a.py"],
                        "removed_security_code": [],
                        "entrypoints_touched": [],
                        "dependencies_touched": [],
                    }
                },
            ),
            ("triage", {"per_file_risk": {"src/a.py": "HIGH"}}),
            ("code_analysis", {"findings": []}),
            ("test_coverage", {"coverage": {}}),
            ("blast_radius", {"blast": {"per_file_callers": {}}}),
        ]:
            await client.call_tool("step", {"action": action_name, "inputs": inputs})

        r = await client.call_tool("step", {"action": "write_report", "inputs": {"report": "skip"}})
        out = json.loads(r.content[0].text)
        assert out.get("error") == "invalid_transition"
        assert "deep_context" in out["valid_next_actions"]
