"""Mellea-inside-Burr Qiskit migration demo: hermetic FSM tests.

Tests cover the deterministic migration checker (pure function,
hits every documented deprecation pattern) plus the FSM behaviour
with ``_call_mellea`` monkey-patched. No Mellea install or Ollama
reachability required.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import mellea_qiskit_migration  # noqa: E402
from mellea_qiskit_migration import (  # noqa: E402
    build_server,
    check_qiskit_migration,
)

# ── deterministic migration checker (pure function) ─────────────────


def test_check_qiskit_migration_clean_code_returns_empty():
    clean = (
        "from qiskit import QuantumCircuit\n"
        "from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2\n"
        "\n"
        "qc = QuantumCircuit(2)\n"
        "qc.h(0); qc.cx(0, 1); qc.measure_all()\n"
        "service = QiskitRuntimeService()\n"
        "backend = service.backend('ibm_kyoto')\n"
        "result = SamplerV2(backend).run([qc]).result()\n"
    )
    assert check_qiskit_migration(clean) == []


def test_check_qiskit_migration_flags_ibmq_import():
    bad = "from qiskit import IBMQ\n"
    issues = check_qiskit_migration(bad)
    assert len(issues) == 1
    assert "IBMQ" in issues[0]["issue"]
    assert "QiskitRuntimeService" in issues[0]["fix"]


def test_check_qiskit_migration_flags_ibmq_load_account():
    bad = "IBMQ.load_account()\n"
    issues = check_qiskit_migration(bad)
    assert any("load_account" in i["issue"] for i in issues)


def test_check_qiskit_migration_flags_execute_call():
    bad = "result = execute(circuit, backend, shots=1024).result()\n"
    issues = check_qiskit_migration(bad)
    assert any("execute" in i["issue"] for i in issues)


def test_check_qiskit_migration_flags_aer_get_backend():
    bad = "sim = Aer.get_backend('qasm_simulator')\n"
    issues = check_qiskit_migration(bad)
    assert any("Aer.get_backend" in i["issue"] for i in issues)


def test_check_qiskit_migration_flags_qasm_simulator():
    bad = "sim = QasmSimulator()\n"
    issues = check_qiskit_migration(bad)
    assert any("QasmSimulator" in i["issue"] for i in issues)


def test_check_qiskit_migration_flags_multiple_patterns_in_one_chunk():
    realistic = (
        "from qiskit import IBMQ, QuantumCircuit\n"
        "IBMQ.load_account()\n"
        "provider = IBMQ.providers()[0]\n"
        "backend = Aer.get_backend('qasm_simulator')\n"
        "result = execute(circuit, backend, shots=1024).result()\n"
    )
    issues = check_qiskit_migration(realistic)
    # All five patterns above should fire.
    assert len(issues) >= 5


# ── FSM-level tests (monkey-patched _call_mellea) ───────────────────


def _patch_mellea(monkeypatch, *responses: dict):
    """Replace ``_call_mellea`` with a queue of canned response dicts."""
    queue = deque(responses)

    async def fake(deprecated_code, loop_budget=3):
        if not queue:
            raise AssertionError("ran out of canned Mellea responses")
        return queue.popleft()

    monkeypatch.setattr(mellea_qiskit_migration, "_call_mellea", fake)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


_DEPRECATED = (
    "from qiskit import IBMQ\nIBMQ.load_account()\nbackend = Aer.get_backend('qasm_simulator')\n"
)

_MIGRATED = (
    "from qiskit_ibm_runtime import QiskitRuntimeService\n"
    "from qiskit_aer import AerSimulator\n"
    "service = QiskitRuntimeService()\n"
    "backend = AerSimulator()\n"
)

_STILL_BAD = (
    # Attempt that still has IBMQ.load_account()
    "from qiskit_ibm_runtime import QiskitRuntimeService\n"
    "IBMQ.load_account()  # forgot to update this line\n"
)


# ── accept_problem validation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_problem_rejects_empty_code():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(client, "accept_problem", deprecated_code="  ", max_repair_rounds=3)
        )
        assert out["error"] == "action_error"
        assert "deprecated_code" in out["error_message"]


@pytest.mark.asyncio
async def test_accept_problem_rejects_zero_max_rounds():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=0)
        )
        assert out["error"] == "action_error"
        assert "max_repair_rounds" in out["error_message"]


# ── happy path: Mellea returns clean migrated code ────────────────


@pytest.mark.asyncio
async def test_happy_path_routes_to_finalize_success(monkeypatch):
    _patch_mellea(
        monkeypatch,
        {
            "repaired_code": _MIGRATED,
            "success": True,
            "samples_tried": 1,
            "validation_trace": [
                {
                    "round": 1,
                    "checks": [
                        {"name": "no commentary", "passed": True, "reason": ""},
                        {"name": "no removed APIs", "passed": True, "reason": ""},
                    ],
                }
            ],
        },
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=3)
        out = _payload(await _step(client, "mellea_repair_loop"))
        assert out["state"]["success"] is True
        assert out["state"]["remaining_issues"] == []
        assert out["valid_next_actions"] == ["finalize_success"]
        out = _payload(await _step(client, "finalize_success"))
        assert out["state"]["final_report"]["status"] == "migrated"
        assert out["state"]["final_report"]["migrated_code"] == _MIGRATED


# ── giveup path: Mellea exhausts budget with issues remaining ─────


@pytest.mark.asyncio
async def test_giveup_path_routes_to_finalize_giveup(monkeypatch):
    _patch_mellea(
        monkeypatch,
        {
            "repaired_code": _STILL_BAD,
            "success": False,  # Mellea's own success says no
            "samples_tried": 3,
            "validation_trace": [
                {
                    "round": 1,
                    "checks": [
                        {
                            "name": "no removed APIs",
                            "passed": False,
                            "reason": "IBMQ.load_account still present",
                        }
                    ],
                },
                {
                    "round": 2,
                    "checks": [
                        {
                            "name": "no removed APIs",
                            "passed": False,
                            "reason": "IBMQ.load_account still present",
                        }
                    ],
                },
                {
                    "round": 3,
                    "checks": [
                        {
                            "name": "no removed APIs",
                            "passed": False,
                            "reason": "IBMQ.load_account still present",
                        }
                    ],
                },
            ],
        },
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=3)
        out = _payload(await _step(client, "mellea_repair_loop"))
        assert out["state"]["success"] is False
        assert len(out["state"]["remaining_issues"]) >= 1
        assert out["valid_next_actions"] == ["finalize_giveup"]
        out = _payload(await _step(client, "finalize_giveup"))
        report = out["state"]["final_report"]
        assert report["status"] == "needs_human"
        assert report["samples_tried"] == 3
        assert len(report["remaining_issues"]) >= 1


# ── transition gating: canonical success overrides Mellea's flag ──


@pytest.mark.asyncio
async def test_canonical_check_overrides_mellea_success_flag(monkeypatch):
    """If Mellea returns success=True but the deterministic checker
    finds remaining deprecated patterns, the FSM trusts the checker
    and routes to giveup."""
    _patch_mellea(
        monkeypatch,
        {
            "repaired_code": _STILL_BAD,
            "success": True,  # Mellea says all good
            "samples_tried": 1,
            "validation_trace": [
                {"round": 1, "checks": [{"name": "no removed APIs", "passed": True, "reason": ""}]}
            ],
        },
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=3)
        out = _payload(await _step(client, "mellea_repair_loop"))
        # Canonical check sees IBMQ.load_account() still there.
        assert out["state"]["success"] is False
        assert out["valid_next_actions"] == ["finalize_giveup"]


@pytest.mark.asyncio
async def test_cannot_finalize_success_when_issues_remain(monkeypatch):
    _patch_mellea(
        monkeypatch,
        {
            "repaired_code": _STILL_BAD,
            "success": False,
            "samples_tried": 3,
            "validation_trace": [],
        },
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=3)
        await _step(client, "mellea_repair_loop")
        out = _payload(await _step(client, "finalize_success"))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["finalize_giveup"]


# ── audit trail surfaces in state ─────────────────────────────────


@pytest.mark.asyncio
async def test_validation_trace_surfaces_in_state(monkeypatch):
    trace = [
        {
            "round": 1,
            "checks": [
                {"name": "no commentary", "passed": False, "reason": "starts with 'Here is'"},
                {"name": "no removed APIs", "passed": False, "reason": "IBMQ still in code"},
            ],
        },
        {
            "round": 2,
            "checks": [
                {"name": "no commentary", "passed": True, "reason": ""},
                {"name": "no removed APIs", "passed": True, "reason": ""},
            ],
        },
    ]
    _patch_mellea(
        monkeypatch,
        {
            "repaired_code": _MIGRATED,
            "success": True,
            "samples_tried": 2,
            "validation_trace": trace,
        },
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "accept_problem", deprecated_code=_DEPRECATED, max_repair_rounds=3)
        out = _payload(await _step(client, "mellea_repair_loop"))
        assert out["state"]["validation_trace"] == trace
        assert out["state"]["samples_tried"] == 2
