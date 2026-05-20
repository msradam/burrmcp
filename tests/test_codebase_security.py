"""Codebase security audit FSM tests.

Mix of real-scanner runs against the shipped vuln_demo tree and
hermetic mocked-subprocess runs for the failure modes (scanner
missing, malformed JSON, timeout). Helpers (CWE map lookup,
remediation table) are exercised as plain unit tests too.

The vuln_demo tree on disk is treated as read-only by the FSM: the
remediation loop applies recorded patches to a copy in a tmpdir.
These tests rely on that guarantee, so a stray failure here that
mutates the demo files would be a serious regression.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import codebase_security
from codebase_security import (
    _BANDIT_CWE_MAP,
    _bandit_cwe,
    _inventory,
    _remediation_for,
    _secrets_cwe,
    _stuck_count,
    build_server,
)

VULN_DEMO = REPO_ROOT / "examples" / "data" / "codebase_security" / "vuln_demo"


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


async def _walk_to_audit_report(client, repo_path: str, max_rescans: int = 3) -> dict[str, Any]:
    """Walk start_audit through produce_audit_report and return the final payload."""
    await _step(client, "start_audit", repo_path=repo_path, max_rescans=max_rescans)
    await _step(client, "inventory_files")
    await _step(client, "run_scans")
    await _step(client, "normalize_findings")
    await _step(client, "triage_findings")
    await _step(client, "rank_findings")
    await _step(client, "assess_risk")
    return _payload(await _step(client, "produce_audit_report"))


async def _walk_after_acknowledge(client) -> dict[str, Any]:
    """After acknowledge_finding, walk the normalised pipeline back to produce_audit_report."""
    await _step(client, "normalize_findings")
    await _step(client, "triage_findings")
    await _step(client, "rank_findings")
    await _step(client, "assess_risk")
    return _payload(await _step(client, "produce_audit_report"))


async def _walk_after_rescan(client) -> dict[str, Any]:
    """After confirm_fixes_applied, run the rescan pipeline back to produce_audit_report."""
    await _step(client, "run_scans")
    await _step(client, "normalize_findings")
    await _step(client, "triage_findings")
    await _step(client, "rank_findings")
    await _step(client, "assess_risk")
    return _payload(await _step(client, "produce_audit_report"))


# unit tests on helpers ───────────────────────────────────────────


def test_bandit_cwe_map_covers_every_documented_rule():
    expected_rules = {
        "B301",
        "B307",
        "B602",
        "B605",
        "B608",
        "B324",
        "B303",
        "B105",
        "B403",
        "B404",
    }
    assert expected_rules <= set(_BANDIT_CWE_MAP.keys())


def test_bandit_cwe_unknown_rule_falls_back_to_cwe_1023():
    cwe, _title, severity = _bandit_cwe("B999", "HIGH")
    assert cwe == "CWE-1023"
    assert severity == "high"


def test_bandit_cwe_unknown_rule_with_garbage_severity_defaults_to_medium():
    cwe, _title, severity = _bandit_cwe("B999", "")
    assert cwe == "CWE-1023"
    assert severity == "medium"


def test_secrets_cwe_known_and_fallback():
    cwe, _title, sev = _secrets_cwe("AWS Access Key")
    assert (cwe, sev) == ("CWE-798", "critical")
    cwe, _title, sev = _secrets_cwe("Brand New Detector")
    assert (cwe, sev) == ("CWE-798", "medium")


def test_remediation_for_known_and_fallback():
    assert "parameterized queries" in _remediation_for("CWE-89")
    assert _remediation_for("CWE-DOES-NOT-EXIST") == _remediation_for("CWE-1023")


def test_inventory_skips_dot_dirs(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "foo.pyc").write_text("")
    inv = _inventory(tmp_path)
    assert "a.py" in inv["py_files"]
    assert all(".git" not in p and "__pycache__" not in p for p in inv["py_files"])


def test_stuck_count_intersects_on_file_line_rule():
    prior = [
        {"file": "a.py", "line": 1, "rule_id": "B602", "severity": "critical"},
        {"file": "b.py", "line": 2, "rule_id": "B608", "severity": "high"},
        {"file": "c.py", "line": 3, "rule_id": "B105", "severity": "medium"},
    ]
    current = [
        {"file": "a.py", "line": 1, "rule_id": "B602", "severity": "critical"},
        {"file": "b.py", "line": 99, "rule_id": "B608", "severity": "high"},
    ]
    assert _stuck_count(prior, current) == 1
    assert _stuck_count(None, current) == 0
    assert _stuck_count([], current) == 0


# real-scanner tests against vuln_demo ─────────────────────────────


@pytest.mark.asyncio
async def test_full_happy_path_audit_against_vuln_demo():
    """Walk start_audit through produce_audit_report and confirm the
    headline numbers from the deliberately-vulnerable demo tree."""
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(VULN_DEMO))
        report = out["state"]["audit_report"]
        assert report["total_findings"] >= 8
        assert report["severity_counts"]["critical"] >= 3
        assert report["audit_decision"] == "review_required"


@pytest.mark.asyncio
async def test_audit_findings_cover_cwes_502_78_798():
    """The shipped demo trips pickle (CWE-502), shell injection (CWE-78),
    and hardcoded secrets (CWE-798). Confirm all three appear."""
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(VULN_DEMO))
        cwes = {f["cwe_id"] for f in out["state"]["findings"]}
        assert "CWE-502" in cwes
        assert "CWE-78" in cwes
        assert "CWE-798" in cwes


@pytest.mark.asyncio
async def test_top_findings_are_ranked_severity_descending():
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(VULN_DEMO))
        top = out["state"]["top_findings"]
        assert len(top) == 5
        ranks = [codebase_security._SEVERITY_RANK[f["severity"]] for f in top]
        assert ranks == sorted(ranks, reverse=True)


@pytest.mark.asyncio
async def test_valid_next_after_report_excludes_confirm_until_patch():
    """produce_audit_report -> review_required exposes propose_patch,
    acknowledge_finding, escalate_to_human; confirm_fixes_applied is
    NOT yet valid because nothing has been proposed or suppressed."""
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(VULN_DEMO))
        valid = set(out["valid_next_actions"])
        assert "propose_patch" in valid
        assert "acknowledge_finding" in valid
        assert "escalate_to_human" in valid
        assert "confirm_fixes_applied" not in valid


@pytest.mark.asyncio
async def test_propose_patch_recorded_and_confirm_becomes_valid():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO))
        out = _payload(
            await _step(
                client,
                "propose_patch",
                file_path="app/serde.py",
                search="return pickle.loads(blob)  # B301",
                replace="return blob  # patched",
                finding_id="serde-cwe502",
            )
        )
        applied = out["state"]["applied_patches"]
        assert len(applied) == 1
        assert applied[0]["file_path"] == "app/serde.py"
        assert applied[0]["proposed_at_round"] == 0
        # propose_patch transitions back to produce_audit_report. Walk
        # one more step to see the post-report valid-next set.
        out = _payload(await _step(client, "produce_audit_report"))
        valid = set(out["valid_next_actions"])
        assert "confirm_fixes_applied" in valid


@pytest.mark.asyncio
async def test_propose_patch_search_not_found_action_errors():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO))
        out = _payload(
            await _step(
                client,
                "propose_patch",
                file_path="app/serde.py",
                search="this string does not appear anywhere",
                replace="x",
            )
        )
        assert out["error"] == "action_error"
        assert "search not found" in out["error_message"]


@pytest.mark.asyncio
async def test_propose_patch_search_multiple_matches_action_errors():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO))
        # "import" appears multiple times in admin.py.
        out = _payload(
            await _step(
                client,
                "propose_patch",
                file_path="app/admin.py",
                search="import",
                replace="# import",
            )
        )
        assert out["error"] == "action_error"
        assert "matches" in out["error_message"]


@pytest.mark.asyncio
async def test_acknowledge_finding_removes_it_from_normalised_set():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO))
        # Acknowledge the bandit B403 advisory on serde.py:3.
        await _step(
            client,
            "acknowledge_finding",
            rule_id="B403",
            file="app/serde.py",
            line=3,
            reason="pickle import is itself only an advisory",
        )
        out = await _walk_after_acknowledge(client)
        findings = out["state"]["findings"]
        for f in findings:
            assert not (
                f["rule_id"] == "B403" and f["file"] == "app/serde.py" and int(f["line"]) == 3
            ), f"B403 at app/serde.py:3 should have been suppressed: {f}"


@pytest.mark.asyncio
async def test_confirm_fixes_applied_rescan_eliminates_fixed_finding():
    """Propose a real fix for the CWE-502 pickle.loads finding, rescan
    against the overlay, and confirm the corresponding finding is gone."""
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(VULN_DEMO))
        initial_findings = out["state"]["findings"]
        cwe502_initial = [f for f in initial_findings if f["cwe_id"] == "CWE-502"]
        assert cwe502_initial, "expected CWE-502 finding in baseline"
        # Patch serde.py to use json.loads on a decoded blob.
        await _step(
            client,
            "propose_patch",
            file_path="app/serde.py",
            search="return pickle.loads(blob)  # B301",
            replace="import json\n    return json.loads(blob.decode())",
        )
        # propose_patch loops back to produce_audit_report. Walk that step
        # so confirm_fixes_applied becomes valid.
        await _step(client, "produce_audit_report")
        await _step(client, "confirm_fixes_applied")
        out = await _walk_after_rescan(client)
        rescan_findings = out["state"]["findings"]
        cwe502_after = [f for f in rescan_findings if f["cwe_id"] == "CWE-502"]
        assert not cwe502_after, f"CWE-502 should be eliminated by the patch; got: {cwe502_after}"
        assert out["state"]["rescan_count"] == 1


@pytest.mark.asyncio
async def test_stuck_detection_after_no_op_patches_blocks_audit():
    """Two no-op patches that don't actually fix anything should
    leave the critical/high findings intact. After the rescan,
    stuck_critical_high jumps; assess_risk routes to blocked."""
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO))
        # No-op patches: change docstrings, doesn't reduce findings.
        await _step(
            client,
            "propose_patch",
            file_path="app/serde.py",
            search='"""Serialization helpers. DO NOT USE: deliberately vulnerable."""',
            replace='"""Serialization helpers. Deliberately vulnerable; do not use."""',
        )
        await _step(client, "produce_audit_report")
        await _step(client, "confirm_fixes_applied")
        out = await _walk_after_rescan(client)
        # At least the critical/high findings in serde.py + admin.py
        # remain. Confirm stuck_critical_high >= 2.
        assert out["state"]["stuck_critical_high"] >= 2
        assert out["state"]["audit_decision"] == "blocked"
        valid = set(out["valid_next_actions"])
        assert valid == {"finalize_blocked"}


@pytest.mark.asyncio
async def test_budget_exhaustion_forces_finalize_blocked():
    """With max_rescans=1 and a no-op rescan that doesn't clear the
    finding set, the audit hits the budget cap and finalize_blocked
    is the only forward move."""
    server = build_server()
    async with Client(server) as client:
        await _walk_to_audit_report(client, str(VULN_DEMO), max_rescans=1)
        # A patch that touches only a comment doesn't reduce findings.
        await _step(
            client,
            "propose_patch",
            file_path="app/db.py",
            search="# CWE-798 (B105 hardcoded password)",
            replace="# hardcoded credential, see CWE-798",
        )
        await _step(client, "produce_audit_report")
        await _step(client, "confirm_fixes_applied")
        out = await _walk_after_rescan(client)
        assert out["state"]["audit_decision"] == "blocked"
        assert out["valid_next_actions"] == ["finalize_blocked"]
        final = _payload(await _step(client, "finalize_blocked"))
        report = final["state"]["audit_report"]
        assert report["status"] == "blocked"
        assert report["reason"] in {"budget exhausted", "stuck critical findings"}


@pytest.mark.asyncio
async def test_clean_codebase_routes_to_finalize_safe(tmp_path):
    """Empty Python tree -> no findings -> safe -> finalize_safe."""
    (tmp_path / "ok.py").write_text("x = 1\n")
    server = build_server()
    async with Client(server) as client:
        out = await _walk_to_audit_report(client, str(tmp_path))
        assert out["state"]["audit_decision"] == "safe"
        assert out["valid_next_actions"] == ["finalize_safe"]
        final = _payload(await _step(client, "finalize_safe"))
        report = final["state"]["audit_report"]
        assert report["status"] == "safe"
        assert report["verdict"] == "clean"


# mocked-subprocess failure modes ──────────────────────────────────


class _BrokenProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        return None


def _patch_subprocess(monkeypatch, factory):
    """Replace asyncio.create_subprocess_exec with a factory call."""

    async def fake_create(*args, **kwargs):
        return factory(args, kwargs)

    monkeypatch.setattr(codebase_security.asyncio, "create_subprocess_exec", fake_create)


@pytest.mark.asyncio
async def test_run_scans_bandit_missing_surfaces_action_error(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")

    async def fake_create(*args, **kwargs):
        raise FileNotFoundError("bandit")

    monkeypatch.setattr(codebase_security.asyncio, "create_subprocess_exec", fake_create)
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", repo_path=str(tmp_path))
        await _step(client, "inventory_files")
        out = _payload(await _step(client, "run_scans"))
        assert out["error"] == "action_error"
        assert "not installed" in out["error_message"]


@pytest.mark.asyncio
async def test_run_scans_malformed_bandit_json_action_errors(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    _patch_subprocess(
        monkeypatch, lambda *_a, **_k: _BrokenProc(stdout=b"not valid json", returncode=1)
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", repo_path=str(tmp_path))
        await _step(client, "inventory_files")
        out = _payload(await _step(client, "run_scans"))
        assert out["error"] == "action_error"
        assert "malformed JSON" in out["error_message"]


@pytest.mark.asyncio
async def test_run_scans_timeout_action_errors(monkeypatch, tmp_path):
    """Drive ``_run_subprocess`` straight into its TimeoutError branch so
    the wrapping ValueError surfaces as an action_error payload. We
    patch the helper itself rather than ``asyncio.wait_for`` so we
    don't also intercept burr-mcp's own per-action timeout wrapper."""
    (tmp_path / "a.py").write_text("x = 1\n")

    class _HangingProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(60)
            return b"", b""

        def kill(self):
            return None

    async def fake_create(*_args, **_kwargs):
        return _HangingProc()

    monkeypatch.setattr(codebase_security, "_SCAN_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(codebase_security.asyncio, "create_subprocess_exec", fake_create)
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_audit", repo_path=str(tmp_path))
        await _step(client, "inventory_files")
        out = _payload(await _step(client, "run_scans"))
        assert out["error"] == "action_error"
        assert "timed out" in out["error_message"]
