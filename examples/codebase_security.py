"""Codebase security audit as an FSM.

A vulnerability auditor that walks deterministic scanners (bandit +
detect-secrets) against a Python tree, normalises every hit to a
shared {tool, file, line, cwe_id, severity, rule_id, snippet,
remediation} shape, ranks by severity, and emits a structured
audit report. The headline is the audit itself; the remediation
loop is where the FSM shape pays off:

    start_audit -> inventory_files -> run_scans -> normalize_findings
        -> triage_findings -> rank_findings -> assess_risk
        -> produce_audit_report
              |
              +---- safe              ----> finalize_safe
              +---- blocked           ----> finalize_blocked
              +---- review_required   ----> propose_patch (loop)
                                       ----> acknowledge_finding (loop)
                                       ----> confirm_fixes_applied
                                              -> run_scans (rescan)
                                       ----> escalate_to_human

Critically: ``propose_patch(file, search, replace)`` does not edit
the codebase on disk. Every proposed patch is stored in state with
the round it was proposed at. When the agent calls
``confirm_fixes_applied``, the next ``run_scans`` copies the repo
into a temp directory, applies the recorded search/replace pairs to
the overlay, and re-scans the overlay. The original tree stays
untouched and the demo is fully re-runnable.

The audit trail is the payoff. Every propose_patch and every
acknowledge_finding (suppression) appends to ``patch_log`` with the
round it happened in, so the resulting audit_report contains a
complete record of what the agent proposed, what it suppressed,
which proposals actually reduced the finding set, and where the
agent gave up (escalated or got blocked by the budget). Reviewers
read the report and the log together; the report tells them what
was wrong, the log tells them how the agent's response evolved.

Stuck-detection: after a rescan, if at least two critical/high
findings remain that were present in the prior scan, the FSM marks
the audit blocked. The agent is not allowed to grind through
budget on patches that are not reducing risk.

Run:

    python examples/codebase_security.py

Then drive it from an MCP client by calling
``start_audit(repo_path="...")`` and walking the legal sequence.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "codebase-security-demo"
_SCAN_TIMEOUT_SECONDS = 30


# CWE maps and remediation table ────────────────────────────────────


_BANDIT_CWE_MAP: dict[str, tuple[str, str, str]] = {
    # rule_id -> (cwe_id, cwe_title, severity)
    "B301": ("CWE-502", "Insecure Deserialization", "critical"),
    "B307": ("CWE-95", "Improper Neutralization (eval)", "critical"),
    "B602": ("CWE-78", "OS Command Injection (shell=True)", "critical"),
    "B605": ("CWE-78", "OS Command Injection (os.system)", "critical"),
    "B608": ("CWE-89", "SQL Injection", "high"),
    "B324": ("CWE-327", "Weak Cryptographic Hash", "medium"),
    "B303": ("CWE-327", "Weak Cryptographic Hash", "medium"),
    "B105": ("CWE-798", "Hardcoded Credentials", "medium"),
    "B403": ("CWE-676", "Use of Dangerous Module Import (pickle)", "info"),
    "B404": ("CWE-676", "Use of Dangerous Module Import (subprocess)", "info"),
}


_SECRETS_CWE_MAP: dict[str, tuple[str, str, str]] = {
    "AWS Access Key": ("CWE-798", "Hardcoded Credentials", "critical"),
    "GitHub Token": ("CWE-798", "Hardcoded Credentials", "critical"),
    "Slack Webhook": ("CWE-798", "Hardcoded Credentials", "high"),
    "Base64 High Entropy String": ("CWE-798", "Hardcoded Credentials", "medium"),
    "Hex High Entropy String": ("CWE-798", "Hardcoded Credentials", "medium"),
    "Secret Keyword": ("CWE-798", "Hardcoded Credentials", "low"),
}


_REMEDIATIONS: dict[str, str] = {
    "CWE-502": "Use json.loads() or a restricted unpickler that allow-lists classes.",
    "CWE-95": "Use ast.literal_eval() for literals; never eval user input.",
    "CWE-78": "Use shlex.split() and pass a list to subprocess with shell=False.",
    "CWE-89": "Use parameterized queries: cursor.execute(sql, (param,)).",
    "CWE-327": (
        "Use hashlib.sha256() or hashlib.blake2b(); pass usedforsecurity=False if intentional."
    ),
    "CWE-798": "Move to environment variables. Rotate the leaked credential immediately.",
    "CWE-676": "Importing this module alone is not a vulnerability; this is an advisory.",
    "CWE-1023": "Review the bandit rule documentation for guidance.",
}


_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


# helpers ──────────────────────────────────────────────────────────


def _bandit_cwe(rule_id: str, fallback_severity: str) -> tuple[str, str, str]:
    """Resolve a bandit rule_id to (cwe_id, cwe_title, severity).

    Unknown rule ids land in the generic CWE-1023 bucket with
    bandit's own severity lowercased as the severity.
    """
    if rule_id in _BANDIT_CWE_MAP:
        return _BANDIT_CWE_MAP[rule_id]
    sev = (fallback_severity or "medium").lower()
    if sev not in _SEVERITY_RANK:
        sev = "medium"
    return ("CWE-1023", "Generic Security Risk", sev)


def _secrets_cwe(secret_type: str) -> tuple[str, str, str]:
    """Resolve a detect-secrets type to (cwe_id, cwe_title, severity)."""
    if secret_type in _SECRETS_CWE_MAP:
        return _SECRETS_CWE_MAP[secret_type]
    return ("CWE-798", "Hardcoded Credentials", "medium")


def _remediation_for(cwe_id: str) -> str:
    return _REMEDIATIONS.get(cwe_id, _REMEDIATIONS["CWE-1023"])


def _relpath(absolute: str, base: Path) -> str:
    """Make a scan-result path relative to ``base``.

    Bandit and detect-secrets emit paths that can be either relative
    to the cwd they ran in or absolute. Normalise so the audit
    report only ever shows repo-relative paths.
    """
    p = Path(absolute)
    base_resolved = base.resolve()
    try:
        if p.is_absolute():
            return str(p.resolve().relative_to(base_resolved))
    except ValueError:
        return str(p)
    # Already relative; if it starts with the base name, strip it.
    parts = list(p.parts)
    base_name = base_resolved.name
    if parts and parts[0] == base_name:
        return str(Path(*parts[1:])) if len(parts) > 1 else ""
    return str(p)


def _inventory(repo: Path) -> dict[str, Any]:
    """Walk ``repo`` skipping __pycache__/.git/.venv. Count file
    extensions and collect repo-relative .py paths.
    """
    skip = {"__pycache__", ".git", ".venv"}
    counts: dict[str, int] = {}
    py_files: list[str] = []
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        for fname in files:
            total += 1
            ext = Path(fname).suffix or "<none>"
            counts[ext] = counts.get(ext, 0) + 1
            if fname.endswith(".py"):
                rel = str(Path(root, fname).relative_to(repo))
                py_files.append(rel)
    py_files.sort()
    return {
        "extension_counts": counts,
        "py_files": py_files,
        "total_files": total,
    }


def _apply_patches_to_overlay(
    repo: Path, overlay: Path, patches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply recorded search/replace patches to files in ``overlay``.

    Returns a list of per-patch outcomes for the audit log. A patch
    whose search string is not found is recorded as an error and
    skipped; we do not abort the rescan on a single bad patch.
    """
    outcomes: list[dict[str, Any]] = []
    for patch in patches:
        rel = patch["file_path"]
        target = overlay / rel
        if not target.exists():
            outcomes.append(
                {
                    "action": "apply_patch",
                    "round": patch.get("proposed_at_round"),
                    "file_path": rel,
                    "error": "file_not_found_in_overlay",
                }
            )
            continue
        text = target.read_text(encoding="utf-8")
        if patch["search"] not in text:
            outcomes.append(
                {
                    "action": "apply_patch",
                    "round": patch.get("proposed_at_round"),
                    "file_path": rel,
                    "error": "search_string_not_found",
                }
            )
            continue
        new_text = text.replace(patch["search"], patch["replace"], 1)
        target.write_text(new_text, encoding="utf-8")
        outcomes.append(
            {
                "action": "apply_patch",
                "round": patch.get("proposed_at_round"),
                "file_path": rel,
                "applied": True,
            }
        )
    return outcomes


async def _run_subprocess(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess with a hard timeout. Raise to surface as action_error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"scanner not installed: {cmd[0]!r}: {exc}") from exc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_SCAN_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        proc.kill()
        raise ValueError(
            f"scanner {' '.join(cmd[:3])!r} timed out after {_SCAN_TIMEOUT_SECONDS}s"
        ) from exc
    return proc.returncode or 0, stdout_b.decode("utf-8"), stderr_b.decode("utf-8")


async def _run_bandit(target: Path) -> dict[str, Any]:
    """Run bandit and return parsed JSON. Bandit exits 1 with findings."""
    rc, stdout, stderr = await _run_subprocess(
        ["uv", "run", "bandit", "-r", str(target), "-f", "json", "--quiet"]
    )
    if rc >= 2:
        raise ValueError(f"bandit exited with code {rc}: {stderr[:300]}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bandit returned malformed JSON: {exc}") from exc


async def _run_detect_secrets(target: Path) -> dict[str, Any]:
    """Run detect-secrets and return parsed JSON."""
    rc, stdout, stderr = await _run_subprocess(
        ["uv", "run", "detect-secrets", "scan", str(target), "--all-files"]
    )
    if rc >= 2:
        raise ValueError(f"detect-secrets exited with code {rc}: {stderr[:300]}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"detect-secrets returned malformed JSON: {exc}") from exc


def _normalize_bandit(bandit_raw: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in bandit_raw.get("results", []):
        rule = r.get("test_id", "")
        cwe_id, cwe_title, severity = _bandit_cwe(rule, r.get("issue_severity", ""))
        out.append(
            {
                "tool": "bandit",
                "file": _relpath(r.get("filename", ""), base),
                "line": int(r.get("line_number", 0)),
                "cwe_id": cwe_id,
                "cwe_title": cwe_title,
                "severity": severity,
                "rule_id": rule,
                "snippet": (r.get("code") or "").strip()[:240],
                "remediation": _remediation_for(cwe_id),
            }
        )
    return out


def _normalize_secrets(secrets_raw: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for filename, items in (secrets_raw.get("results") or {}).items():
        for it in items:
            secret_type = it.get("type", "")
            cwe_id, cwe_title, severity = _secrets_cwe(secret_type)
            out.append(
                {
                    "tool": "detect-secrets",
                    "file": _relpath(filename, base),
                    "line": int(it.get("line_number", 0)),
                    "cwe_id": cwe_id,
                    "cwe_title": cwe_title,
                    "severity": severity,
                    "rule_id": secret_type,
                    "snippet": "",
                    "remediation": _remediation_for(cwe_id),
                }
            )
    return out


def _is_suppressed(finding: dict[str, Any], suppressed: list[dict[str, Any]]) -> bool:
    """Match a finding against the suppression list on (rule_id, file, line)."""
    for s in suppressed:
        if (
            s.get("rule_id") == finding["rule_id"]
            and s.get("file") == finding["file"]
            and int(s.get("line", -1)) == int(finding["line"])
        ):
            return True
    return False


def _stuck_count(prior_keys: list[dict[str, Any]] | None, current: list[dict[str, Any]]) -> int:
    """Count critical/high findings present in BOTH prior and current sets.

    Identity is (file, line, rule_id). The keys list is a snapshot
    of the prior pass's findings stored at the start of run_scans
    before re-running the scanners.
    """
    if not prior_keys:
        return 0
    prior_keyed = {
        (p["file"], int(p["line"]), p["rule_id"])
        for p in prior_keys
        if p.get("severity") in {"critical", "high"}
    }
    if not prior_keyed:
        return 0
    current_keyed = {
        (c["file"], int(c["line"]), c["rule_id"])
        for c in current
        if c["severity"] in {"critical", "high"}
    }
    return len(prior_keyed & current_keyed)


# actions ──────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "repo_path",
        "file_inventory",
        "bandit_raw",
        "secrets_raw",
        "findings",
        "prior_findings_keys",
        "severity_counts",
        "top_findings",
        "applied_patches",
        "suppressed_findings",
        "patch_log",
        "audit_decision",
        "rescan_count",
        "stuck_critical_high",
        "max_rescans",
        "audit_report",
        "log",
    ],
)
def start_audit(state: State, repo_path: str, max_rescans: int = 3) -> State:
    """Begin an audit of a Python codebase.

    Args:
        repo_path: Absolute path to the repository root.
        max_rescans: Hard cap on rescan rounds before the FSM
            forces a finalize_blocked verdict. Default 3.
    """
    p = Path(repo_path).expanduser()
    if not p.exists():
        raise ValueError(f"repo_path does not exist: {repo_path}")
    if not p.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")
    if max_rescans < 0:
        raise ValueError(f"max_rescans must be non-negative; got {max_rescans}")
    return state.update(
        repo_path=str(p.resolve()),
        file_inventory={},
        bandit_raw=None,
        secrets_raw=None,
        findings=[],
        prior_findings_keys=None,
        severity_counts={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        top_findings=[],
        applied_patches=[],
        suppressed_findings=[],
        patch_log=[],
        audit_decision="auditing",
        rescan_count=0,
        stuck_critical_high=0,
        max_rescans=int(max_rescans),
        audit_report=None,
        log=[f"Audit started on {p.resolve()} (max_rescans={max_rescans})."],
    )


@action(reads=["repo_path", "log"], writes=["file_inventory", "log"])
def inventory_files(state: State) -> State:
    """Walk the repo to count files by extension and list .py paths."""
    inv = _inventory(Path(state["repo_path"]))
    return state.update(
        file_inventory=inv,
        log=[
            *state["log"],
            f"Inventoried {inv['total_files']} files; {len(inv['py_files'])} Python.",
        ],
    )


@action(
    reads=[
        "repo_path",
        "applied_patches",
        "suppressed_findings",
        "rescan_count",
        "findings",
        "log",
    ],
    writes=[
        "bandit_raw",
        "secrets_raw",
        "prior_findings_keys",
        "rescan_count",
        "patch_log",
        "log",
    ],
)
async def run_scans(state: State) -> State:
    """Run bandit and detect-secrets.

    Rescans copy the repo to a temp overlay, apply recorded
    search/replace patches there, and scan the overlay. The
    original codebase on disk is never modified.
    """
    repo = Path(state["repo_path"])
    applied_patches: list[dict[str, Any]] = list(state.get("applied_patches") or [])
    suppressed: list[dict[str, Any]] = list(state.get("suppressed_findings") or [])
    rescan_count = int(state.get("rescan_count") or 0)
    is_followup = bool(applied_patches) or bool(suppressed)
    prior_keys = [
        {
            "file": f["file"],
            "line": int(f["line"]),
            "rule_id": f["rule_id"],
            "severity": f["severity"],
        }
        for f in (state.get("findings") or [])
    ]
    log_lines = list(state["log"])
    patch_log = list(state.get("patch_log") or [])
    use_overlay = bool(applied_patches) and is_followup
    loop = asyncio.get_event_loop()
    if use_overlay:
        with tempfile.TemporaryDirectory(prefix="circe_audit_overlay_") as td:
            overlay_root = Path(td) / repo.name
            await loop.run_in_executor(None, shutil.copytree, str(repo), str(overlay_root))
            apply_outcomes = _apply_patches_to_overlay(repo, overlay_root, applied_patches)
            patch_log.extend(apply_outcomes)
            bandit_raw = await _run_bandit(overlay_root)
            secrets_raw = await _run_detect_secrets(overlay_root)
            # Re-anchor scanner output paths to the original repo root.
            for r in bandit_raw.get("results", []):
                r["filename"] = _relpath(r.get("filename", ""), overlay_root)
            new_results: dict[str, list[dict[str, Any]]] = {}
            for fname, items in (secrets_raw.get("results") or {}).items():
                new_results[_relpath(fname, overlay_root)] = items
            secrets_raw["results"] = new_results
    else:
        bandit_raw = await _run_bandit(repo)
        secrets_raw = await _run_detect_secrets(repo)
    if is_followup:
        rescan_count += 1
    log_lines.append(
        f"Scans complete (rescan_count={rescan_count}, overlay={'yes' if use_overlay else 'no'})."
    )
    return state.update(
        bandit_raw=bandit_raw,
        secrets_raw=secrets_raw,
        prior_findings_keys=prior_keys,
        rescan_count=rescan_count,
        patch_log=patch_log,
        log=log_lines,
    )


@action(
    reads=[
        "repo_path",
        "bandit_raw",
        "secrets_raw",
        "suppressed_findings",
        "prior_findings_keys",
        "log",
    ],
    writes=["findings", "stuck_critical_high", "log"],
)
def normalize_findings(state: State) -> State:
    """Normalise scanner output to the shared finding shape.

    Applies the suppression filter and computes how many critical/
    high findings persisted across the most recent rescan.
    """
    base = Path(state["repo_path"])
    bandit_raw = state.get("bandit_raw") or {}
    secrets_raw = state.get("secrets_raw") or {}
    suppressed = list(state.get("suppressed_findings") or [])
    raw_findings = _normalize_bandit(bandit_raw, base) + _normalize_secrets(secrets_raw, base)
    filtered = [f for f in raw_findings if not _is_suppressed(f, suppressed)]
    stuck = _stuck_count(state.get("prior_findings_keys"), filtered)
    return state.update(
        findings=filtered,
        stuck_critical_high=stuck,
        log=[
            *state["log"],
            f"Normalised {len(filtered)} findings ({len(raw_findings) - len(filtered)} "
            f"suppressed); stuck critical/high = {stuck}.",
        ],
    )


@action(reads=["findings", "log"], writes=["severity_counts", "log"])
def triage_findings(state: State) -> State:
    """Count findings by severity."""
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in state["findings"]:
        sev = f.get("severity", "info")
        if sev in counts:
            counts[sev] += 1
        else:
            counts["info"] += 1
    return state.update(
        severity_counts=counts,
        log=[*state["log"], f"Triage: {counts}."],
    )


@action(reads=["findings", "log"], writes=["top_findings", "log"])
def rank_findings(state: State) -> State:
    """Sort findings by severity descending; tie-break by file then line."""

    def _key(f: dict[str, Any]) -> tuple[int, str, int]:
        return (-_SEVERITY_RANK.get(f["severity"], 0), f["file"], int(f["line"]))

    ordered = sorted(state["findings"], key=_key)
    top = ordered[:5]
    return state.update(
        top_findings=top,
        log=[*state["log"], f"Ranked {len(ordered)} findings; top {len(top)} captured."],
    )


@action(
    reads=[
        "severity_counts",
        "rescan_count",
        "max_rescans",
        "stuck_critical_high",
        "log",
    ],
    writes=["audit_decision", "log"],
)
def assess_risk(state: State) -> State:
    """Decide safe / blocked / review_required from counts and history."""
    counts = state["severity_counts"]
    rescan_count = int(state.get("rescan_count") or 0)
    max_rescans = int(state.get("max_rescans") or 0)
    stuck = int(state.get("stuck_critical_high") or 0)
    no_serious = counts["critical"] + counts["high"] + counts["medium"] == 0
    if no_serious:
        decision = "safe"
    elif rescan_count >= max_rescans or (stuck >= 2 and rescan_count > 0):
        decision = "blocked"
    else:
        decision = "review_required"
    return state.update(
        audit_decision=decision,
        log=[
            *state["log"],
            f"Risk assessment: {decision} (rescan={rescan_count}/{max_rescans}, stuck={stuck}).",
        ],
    )


@action(
    reads=[
        "repo_path",
        "rescan_count",
        "findings",
        "severity_counts",
        "top_findings",
        "audit_decision",
        "patch_log",
        "suppressed_findings",
        "applied_patches",
        "stuck_critical_high",
        "log",
    ],
    writes=["audit_report", "log"],
)
def produce_audit_report(state: State) -> State:
    """Assemble the audit report. Routes out on audit_decision."""
    report: dict[str, Any] = {
        "repo_path": state["repo_path"],
        "rescan_count": int(state.get("rescan_count") or 0),
        "total_findings": len(state["findings"]),
        "severity_counts": dict(state["severity_counts"]),
        "top_findings": list(state["top_findings"]),
        "all_findings": list(state["findings"]),
        "audit_decision": state["audit_decision"],
        "patch_log": list(state.get("patch_log") or []),
        "suppressed_findings": list(state.get("suppressed_findings") or []),
        "applied_patches": list(state.get("applied_patches") or []),
        "stuck_critical_high": int(state.get("stuck_critical_high") or 0),
    }
    return state.update(
        audit_report=report,
        log=[
            *state["log"],
            f"Audit report assembled: {report['total_findings']} findings, "
            f"decision={state['audit_decision']}.",
        ],
    )


@action(
    reads=["repo_path", "applied_patches", "patch_log", "rescan_count", "log"],
    writes=["applied_patches", "patch_log", "log"],
)
def propose_patch(
    state: State,
    file_path: str,
    search: str,
    replace: str,
    finding_id: str = "",
) -> State:
    """Record a search/replace patch. Does not write to disk.

    Args:
        file_path: Path to the file under the repo root, either
            absolute or repo-relative.
        search: Literal text to replace. Must appear exactly once
            in the target file.
        replace: Replacement text.
        finding_id: Optional reference to the finding this patch is
            meant to fix; gets stored on the patch record.
    """
    repo = Path(state["repo_path"]).resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.resolve()
    try:
        rel = candidate.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"file_path is outside repo_path: {file_path}") from exc
    if not candidate.is_file():
        raise ValueError(f"file_path is not a file under repo_path: {file_path}")
    if not search:
        raise ValueError("search must not be empty")
    text = candidate.read_text(encoding="utf-8")
    occurrences = text.count(search)
    if occurrences == 0:
        raise ValueError(f"search not found in {rel}")
    if occurrences > 1:
        raise ValueError(
            f"search matches {occurrences} times in {rel}; "
            "make it unique with more surrounding context"
        )
    rescan_count = int(state.get("rescan_count") or 0)
    patch_record = {
        "file_path": str(rel),
        "search": search,
        "replace": replace,
        "proposed_at_round": rescan_count,
        "finding_id_ref": finding_id,
    }
    log_entry = {
        "action": "propose_patch",
        "round": rescan_count,
        "file_path": str(rel),
        "search_preview": search[:60],
        "replace_preview": replace[:60],
    }
    return state.update(
        applied_patches=[*(state.get("applied_patches") or []), patch_record],
        patch_log=[*(state.get("patch_log") or []), log_entry],
        log=[*state["log"], f"Proposed patch for {rel} (round={rescan_count})."],
    )


@action(
    reads=["suppressed_findings", "patch_log", "rescan_count", "log"],
    writes=["suppressed_findings", "patch_log", "log"],
)
def acknowledge_finding(
    state: State,
    rule_id: str,
    file: str,
    line: int,
    reason: str = "",
) -> State:
    """Mark a finding as known-OK and re-normalise.

    Args:
        rule_id: Scanner rule id (e.g. ``B403``, ``Secret Keyword``).
        file: Repo-relative path of the file containing the finding.
        line: 1-based line number of the finding.
        reason: Free-text justification recorded in the audit log.
    """
    rescan_count = int(state.get("rescan_count") or 0)
    suppression = {
        "rule_id": rule_id,
        "file": file,
        "line": int(line),
        "reason": reason,
        "suppressed_at_round": rescan_count,
    }
    log_entry = {
        "action": "acknowledge",
        "round": rescan_count,
        "rule_id": rule_id,
        "file": file,
        "line": int(line),
        "reason": reason,
    }
    return state.update(
        suppressed_findings=[*(state.get("suppressed_findings") or []), suppression],
        patch_log=[*(state.get("patch_log") or []), log_entry],
        log=[
            *state["log"],
            f"Acknowledged {rule_id} at {file}:{line} (round={rescan_count}).",
        ],
    )


@action(reads=["log"], writes=["log"])
def confirm_fixes_applied(state: State) -> State:
    """Trigger a rescan against the overlay with recorded patches."""
    return state.update(
        log=[
            *state["log"],
            "Fixes confirmed; rescanning with patch overlay.",
        ],
    )


@action(
    reads=["audit_report", "log"],
    writes=["audit_decision", "audit_report", "log"],
)
def escalate_to_human(state: State) -> State:
    """Terminal: hand off to a human reviewer."""
    report = dict(state["audit_report"] or {})
    report["status"] = "escalated"
    report["audit_decision"] = "escalated"
    return state.update(
        audit_decision="escalated",
        audit_report=report,
        log=[*state["log"], "Escalated to human reviewer."],
    )


@action(
    reads=["audit_report", "rescan_count", "log"],
    writes=["audit_decision", "audit_report", "log"],
)
def finalize_safe(state: State) -> State:
    """Terminal: codebase is clean (or remediated to clean)."""
    report = dict(state["audit_report"] or {})
    verdict = "remediated" if int(state.get("rescan_count") or 0) > 0 else "clean"
    report["status"] = "safe"
    report["verdict"] = verdict
    report["audit_decision"] = "safe"
    return state.update(
        audit_decision="safe",
        audit_report=report,
        log=[*state["log"], f"Audit complete: safe ({verdict})."],
    )


@action(
    reads=["audit_report", "rescan_count", "max_rescans", "stuck_critical_high", "log"],
    writes=["audit_decision", "audit_report", "log"],
)
def finalize_blocked(state: State) -> State:
    """Terminal: budget exhausted or stuck on critical findings."""
    report = dict(state["audit_report"] or {})
    rescan_count = int(state.get("rescan_count") or 0)
    max_rescans = int(state.get("max_rescans") or 0)
    stuck = int(state.get("stuck_critical_high") or 0)
    if stuck >= 2 and rescan_count > 0:
        reason = "stuck critical findings"
    elif rescan_count >= max_rescans:
        reason = "budget exhausted"
    else:
        reason = "blocked"
    report["status"] = "blocked"
    report["reason"] = reason
    report["audit_decision"] = "blocked"
    return state.update(
        audit_decision="blocked",
        audit_report=report,
        log=[*state["log"], f"Audit complete: blocked ({reason})."],
    )


# graph ────────────────────────────────────────────────────────────


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_audit=start_audit,
            inventory_files=inventory_files,
            run_scans=run_scans,
            normalize_findings=normalize_findings,
            triage_findings=triage_findings,
            rank_findings=rank_findings,
            assess_risk=assess_risk,
            produce_audit_report=produce_audit_report,
            propose_patch=propose_patch,
            acknowledge_finding=acknowledge_finding,
            confirm_fixes_applied=confirm_fixes_applied,
            escalate_to_human=escalate_to_human,
            finalize_safe=finalize_safe,
            finalize_blocked=finalize_blocked,
        )
        .with_transitions(
            ("start_audit", "inventory_files"),
            ("inventory_files", "run_scans"),
            ("run_scans", "normalize_findings"),
            ("normalize_findings", "triage_findings"),
            ("triage_findings", "rank_findings"),
            ("rank_findings", "assess_risk"),
            ("assess_risk", "produce_audit_report"),
            (
                "produce_audit_report",
                "finalize_safe",
                Condition.expr("audit_decision == 'safe'"),
            ),
            (
                "produce_audit_report",
                "finalize_blocked",
                Condition.expr("audit_decision == 'blocked'"),
            ),
            (
                "produce_audit_report",
                "propose_patch",
                Condition.expr("audit_decision == 'review_required'"),
            ),
            (
                "produce_audit_report",
                "acknowledge_finding",
                Condition.expr("audit_decision == 'review_required'"),
            ),
            (
                "produce_audit_report",
                "confirm_fixes_applied",
                Condition.expr(
                    "audit_decision == 'review_required' and "
                    "(len(applied_patches) + len(suppressed_findings)) > 0"
                ),
            ),
            (
                "produce_audit_report",
                "escalate_to_human",
                Condition.expr("audit_decision == 'review_required'"),
            ),
            ("propose_patch", "produce_audit_report"),
            ("acknowledge_finding", "normalize_findings"),
            ("confirm_fixes_applied", "run_scans"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            repo_path="",
            file_inventory={},
            bandit_raw=None,
            secrets_raw=None,
            findings=[],
            prior_findings_keys=None,
            severity_counts={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            top_findings=[],
            applied_patches=[],
            suppressed_findings=[],
            patch_log=[],
            audit_decision="auditing",
            rescan_count=0,
            stuck_critical_high=0,
            max_rescans=3,
            audit_report=None,
            log=[],
        )
        .with_entrypoint("start_audit")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="codebase-security",
        instructions=(
            "Vulnerability-audit FSM. Walks bandit and detect-secrets "
            "against a Python codebase, normalises findings to a shared "
            "shape with CWE ids and remediation, ranks by severity, and "
            "emits a structured audit report. Start every session with "
            "start_audit(repo_path, max_rescans). The remediation loop "
            "is patch-by-search-replace: call propose_patch(file_path, "
            "search, replace) one or more times to record fixes (the "
            "codebase on disk is never modified; every patch and "
            "suppression is appended to patch_log), optionally "
            "acknowledge_finding(rule_id, file, line, reason) to "
            "suppress known-OK hits, then confirm_fixes_applied to "
            "trigger a rescan that copies the repo to a tmpdir, applies "
            "the recorded patches there, and re-runs the scanners. The "
            "loop caps at max_rescans (default 3); when at least two "
            "critical/high findings persist across a rescan the FSM "
            "forces finalize_blocked. The original codebase stays "
            "untouched, so the demo is fully re-runnable."
        ),
        action_timeout_seconds=60,
    )


if __name__ == "__main__":
    build_server().run()
