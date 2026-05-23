"""Skill-to-FSM: web app security audit as caller-LLM prompts.

A real Claude Code SKILL decomposed into a Burr FSM whose actions
emit prompts for the *caller* LLM (Opus, Sonnet, Granite, whoever
is driving BurrMCP through an MCP client). The FSM does not call any
LLM itself; it stores the caller's structured findings in state and
gates which prompt-phase is reachable next.

Source SKILL: ``examples/skills/security-audit/SKILL.md`` (MIT,
github.com/shaxbozaka/security-audit). The phases below are the
same ones the SKILL walks through: context detection, source-code
checklist (INSIDE mode), black-box checklist (OUTSIDE mode),
infra sweep, rate-limit deep-dive, advisory writing.

Shape:

    start_audit(target, mode, authorization_source=None)
      -> record_context(findings)
      -> source_review (INSIDE or BOTH only)
      -> blackbox_review (OUTSIDE or BOTH only)
      -> infra_sweep
      -> rate_limit_deep_dive
      -> write_advisory (terminal)

What the FSM gives the SKILL:

* Each phase becomes a separate visible step in ``burr://history``
  and ``burr://trace``. The audit trail is the artifact: every
  prompt the caller LLM was given + every set of structured
  findings it returned.
* OUTSIDE / BOTH mode require ``authorization_source`` because the
  SKILL says "before probing production from outside, you need
  authorization in writing". The action-level validator refuses
  the start_audit call without it.
* INSIDE mode skips ``blackbox_review``; OUTSIDE skips
  ``source_review``; BOTH runs both, source first.
* The agent (caller LLM) reads ``state.current_prompt`` after each
  step. The prompt tells it which checklist to run, with the
  cheap-first ordering the SKILL recommends.
* Same SKILL, any LLM. The prompts are model-agnostic. Whatever's
  driving BurrMCP processes them.

Pure FSM. No server-side LLM calls, no shellouts, no scanners.
Complementary to ``codebase_security.py`` (which runs deterministic
bandit + detect-secrets): that one is "scanners find findings",
this one is "agent applies SKILL's procedure under FSM-enforced
order".

Run:

    python examples/skill_security_audit.py
"""

from __future__ import annotations

from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "skill-security-audit-demo"

_VALID_MODES = {"INSIDE", "OUTSIDE", "BOTH"}
_MODES_REQUIRING_AUTHORIZATION = {"OUTSIDE", "BOTH"}


# ── prompt templates (drawn from the security-audit SKILL) ─────────


_PROMPT_CONTEXT = """\
You are conducting a security audit of `{target}`. Mode: `{mode}`.

PHASE 1 of 6: CONTEXT DETECTION.

Examine what you can observe about the target and report:
- Technology stack (language, framework, runtime, dependencies, container base)
- Attack surface (entry points, exposed endpoints, file paths, auth flows)
- Visible defenses (auth, validation, rate limits, security headers, CSP)
- Deployment context (cloud provider hints, CDN, container traces, error pages)

Call `record_context(findings={{...}})` with a dict of your observations.
If you can't observe something (e.g., no filesystem access in OUTSIDE mode),
note it explicitly so the audit trail records what was unavailable.
"""


_PROMPT_SOURCE_INSIDE = """\
PHASE 2 of 6: SOURCE-CODE CHECKLIST (INSIDE mode).

Read the code before doing anything else. Every bug found here is free
(no prod traffic, no scope questions). Work in this order, cheap first:

1. Dependency CVEs + version pins
   - `npm audit` / `pip-audit` / `safety check`
   - Red flags: :latest image tags, missing lockfiles, suspect package names

2. Secrets leaked into the repo
   - grep for SECRET / TOKEN / KEY / PASSWORD assignments with long values
   - `git log -p --all -S 'BEGIN RSA PRIVATE KEY' -S 'AKIA' -S 'sk-' -S 'xoxb-'`
   - .env files in git history
   - Client-exposed prefixed secrets (NEXT_PUBLIC_, VITE_, REACT_APP_)

3. Authentication
   - Password hashing (md5, sha1, bcrypt.hashSync, all smells)
   - Session/cookie flags (httpOnly, secure, sameSite)
   - JWT signing key handling

4. Authorization / IDOR
   - Object-level access checks; do controllers fetch by user-owned id

5. Injection (SQL, NoSQL, command, prompt, template)
   - String interpolation in queries, shell=True, eval/exec, untrusted templating

6. SSRF
   - Outbound HTTP from user-controlled URLs without allow-list

7. Crypto misuse
   - Static IVs, ECB mode, weak random, JWT alg confusion

8. File handling
   - Path traversal in uploads/downloads, unrestricted file types

9. Rate limits + cost-inflation DoS
   - Endpoints with no throttling; expensive operations on unauth paths

10. Docker / container config
    - Running as root, exposed management ports, /var/run/docker.sock mounts

For each finding, capture: file path, line number, CWE id, severity,
short description, suggested fix. Call `source_review(findings=[...])`
with a list of finding dicts.
"""


_PROMPT_BLACKBOX = """\
PHASE 3 of 6: BLACK-BOX CHECKLIST (OUTSIDE mode).

You have a URL or hostname. Authorization on file:
`{authorization_source}`. Probe in this order:

1. Subdomain enumeration (subfinder, amass, certificate transparency)
2. Port scan (nmap top ports, especially admin / mgmt / db ports)
3. TLS + DNS hygiene (cert validity, weak ciphers, missing CAA, SPF/DKIM/DMARC)
4. Admin-panel leakage (predictable paths: /admin, /api/admin, /.git, /.env)
5. Error-shape oracle (do 401 / 403 / 404 differ by content length or body)
6. Version fingerprint (server header, framework defaults, X-Powered-By)
7. Auth probe (default credentials, response timing on valid vs invalid users)
8. Rate-limit probe (does the endpoint enforce; XFF spoof; per-account vs per-IP)

DO NOT exploit anything destructive. DO NOT exfiltrate. Stop at the
proof-of-existence stage; the advisory is what carries the bug, not
the demo. For each finding capture: url, technique, observation,
CWE id, severity, repro steps. Call `blackbox_review(findings=[...])`.
"""


_PROMPT_INFRA_SWEEP = """\
PHASE 4 of 6: SERVER / INFRA SWEEP.

Regardless of mode, sweep the deployment surface:

1. Management ports (SSH, RDP, k8s API, etcd, redis, postgres) reachable
   from the public internet
2. Cloud metadata service (169.254.169.254) reachable from app via SSRF
3. CDN bypass (origin IP discoverable via cert transparency or DNS history)
4. Backup files (.bak, .swp, .old, .zip, db dumps) on the public web root
5. Container image CVEs (trivy, grype) for the running image
6. Public S3 buckets / open object storage
7. Logging / monitoring with credentials in clear
8. Open CORS (`*` for credentialed endpoints)

Call `infra_sweep(findings=[...])` with whatever you found, structured
the same way as the prior phases.
"""


_PROMPT_RATE_LIMIT = """\
PHASE 5 of 6: RATE-LIMIT DEEP-DIVE.

Rate-limit bugs are subtle and high-impact. Drill in:

1. Distributed-counter vs in-memory: rate limiter behind a load balancer
   that spreads requests across pods can let you hit N * pods / second
2. X-Forwarded-For trust: does the rate limiter parse XFF and trust it?
   if so you can rotate identities cheaply
3. Cost-inflation DoS: endpoints that do expensive work (LLM calls, DB
   joins, image processing) without per-user accounting
4. Account-rotation arbitrage: limit per-account but registration is free
5. Auth path itself: is the LOGIN endpoint rate limited? what about
   password reset?
6. Reset-window timing: does the counter reset on the clock-hour
   boundary, letting an attacker line up bursts?

Call `rate_limit_deep_dive(findings=[...])` with what you observed.
"""


_PROMPT_ADVISORY = """\
PHASE 6 of 6: WRITE THE ADVISORY.

Compile every finding from the previous phases into a structured
security advisory. Use this shape:

# {target}: security audit advisory

## Summary
- N findings: <breakdown by severity>
- Highest severity: <critical|high|medium|low>
- Audit mode: {mode}
- Scope source: {authorization_source}

## Findings
For each finding, a section with:
- Title
- Severity (critical/high/medium/low/info)
- CWE id
- File or URL + line
- Description
- Reproduction (commands / requests)
- Suggested fix
- References (CVE id if known, OWASP rule id)

## Disclosure path
- GHSA collaborator invite if the project lives on GitHub
- Direct DM to maintainer otherwise
- Embargoed disclosure timeline (90 days default)

Call `write_advisory(advisory="...")` with the full markdown text.
The audit terminates here.
"""


# ── actions (each emits a prompt; no LLM call) ─────────────────────


@action(
    reads=[],
    writes=[
        "target",
        "mode",
        "authorization_source",
        "context_findings",
        "source_findings",
        "blackbox_findings",
        "infra_findings",
        "rate_limit_findings",
        "advisory",
        "current_prompt",
        "log",
    ],
)
async def start_audit(
    state: State,
    target: str,
    mode: str,
    authorization_source: str | None = None,
) -> State:
    """Start the audit. Validates mode and (for OUTSIDE/BOTH) the
    authorization-source argument the SKILL requires.

    Args:
        target: Repo path, URL, or hostname under audit.
        mode: One of "INSIDE", "OUTSIDE", "BOTH". INSIDE = read-only
            filesystem access to source. OUTSIDE = URL/hostname only.
            BOTH = filesystem + live deployment.
        authorization_source: Required for OUTSIDE and BOTH. A
            human-readable scope-artefact reference: maintainer DM
            URL, GHSA collaborator invite id, bug bounty scope URL.
            The SKILL says "before probing production from outside,
            you need authorization in writing".
    """
    if not target.strip():
        raise ValueError("target must not be empty")
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}; got {mode!r}")
    if mode in _MODES_REQUIRING_AUTHORIZATION and not (authorization_source or "").strip():
        raise ValueError(
            f"mode={mode!r} requires authorization_source; the SKILL says "
            "you must have written authorization before probing production"
        )
    prompt = _PROMPT_CONTEXT.format(target=target, mode=mode)
    return state.update(
        target=target,
        mode=mode,
        authorization_source=authorization_source or "",
        context_findings={},
        source_findings=[],
        blackbox_findings=[],
        infra_findings=[],
        rate_limit_findings=[],
        advisory=None,
        current_prompt=prompt,
        log=[f"Audit started: target={target!r}, mode={mode}"],
    )


@action(
    reads=["mode", "authorization_source", "log"],
    writes=["context_findings", "current_prompt", "log"],
)
async def record_context(state: State, findings: dict[str, Any]) -> State:
    """Stash context-detection findings from the agent and emit the
    next phase's prompt.

    Branches on mode: INSIDE/BOTH start with source review; OUTSIDE
    skips straight to black-box.
    """
    mode = state["mode"]
    if mode in {"INSIDE", "BOTH"}:
        next_prompt = _PROMPT_SOURCE_INSIDE
    else:
        next_prompt = _PROMPT_BLACKBOX.format(
            authorization_source=state["authorization_source"] or "(unset)"
        )
    return state.update(
        context_findings=findings,
        current_prompt=next_prompt,
        log=[*state["log"], f"Context recorded: {len(findings)} key(s)"],
    )


@action(
    reads=["mode", "authorization_source", "log"],
    writes=["source_findings", "current_prompt", "log"],
)
async def source_review(state: State, findings: list[dict[str, Any]]) -> State:
    """Stash source-code findings and emit the next phase's prompt.

    Branches on mode: BOTH continues into black-box; INSIDE skips to
    infra sweep.
    """
    mode = state["mode"]
    if mode == "BOTH":
        next_prompt = _PROMPT_BLACKBOX.format(
            authorization_source=state["authorization_source"] or "(unset)"
        )
    else:
        next_prompt = _PROMPT_INFRA_SWEEP
    return state.update(
        source_findings=list(findings or []),
        current_prompt=next_prompt,
        log=[*state["log"], f"Source review recorded: {len(findings or [])} finding(s)"],
    )


@action(
    reads=["log"],
    writes=["blackbox_findings", "current_prompt", "log"],
)
async def blackbox_review(state: State, findings: list[dict[str, Any]]) -> State:
    """Stash black-box findings and emit the infra-sweep prompt."""
    return state.update(
        blackbox_findings=list(findings or []),
        current_prompt=_PROMPT_INFRA_SWEEP,
        log=[*state["log"], f"Blackbox review recorded: {len(findings or [])} finding(s)"],
    )


@action(
    reads=["log"],
    writes=["infra_findings", "current_prompt", "log"],
)
async def infra_sweep(state: State, findings: list[dict[str, Any]]) -> State:
    """Stash infra findings and emit the rate-limit deep-dive prompt."""
    return state.update(
        infra_findings=list(findings or []),
        current_prompt=_PROMPT_RATE_LIMIT,
        log=[*state["log"], f"Infra sweep recorded: {len(findings or [])} finding(s)"],
    )


@action(
    reads=["target", "mode", "authorization_source", "log"],
    writes=["rate_limit_findings", "current_prompt", "log"],
)
async def rate_limit_deep_dive(state: State, findings: list[dict[str, Any]]) -> State:
    """Stash rate-limit findings and emit the advisory-writing prompt."""
    next_prompt = _PROMPT_ADVISORY.format(
        target=state["target"],
        mode=state["mode"],
        authorization_source=state["authorization_source"] or "(internal audit)",
    )
    return state.update(
        rate_limit_findings=list(findings or []),
        current_prompt=next_prompt,
        log=[*state["log"], f"Rate-limit deep-dive recorded: {len(findings or [])} finding(s)"],
    )


@action(
    reads=[
        "target",
        "mode",
        "context_findings",
        "source_findings",
        "blackbox_findings",
        "infra_findings",
        "rate_limit_findings",
        "log",
    ],
    writes=["advisory", "audit_summary", "current_prompt", "log"],
)
async def write_advisory(state: State, advisory: str) -> State:
    """Terminal: stash the final advisory and build a summary count
    across all phases."""
    if not advisory.strip():
        raise ValueError("advisory must not be empty")
    summary = {
        "target": state["target"],
        "mode": state["mode"],
        "total_findings": (
            len(state["source_findings"])
            + len(state["blackbox_findings"])
            + len(state["infra_findings"])
            + len(state["rate_limit_findings"])
        ),
        "findings_per_phase": {
            "source": len(state["source_findings"]),
            "blackbox": len(state["blackbox_findings"]),
            "infra": len(state["infra_findings"]),
            "rate_limit": len(state["rate_limit_findings"]),
        },
    }
    return state.update(
        advisory=advisory,
        audit_summary=summary,
        current_prompt="Audit complete. Final advisory in state.advisory.",
        log=[*state["log"], f"Advisory written ({len(advisory)} chars). Audit complete."],
    )


# ── graph ──────────────────────────────────────────────────────────


_IS_INSIDE_OR_BOTH = Condition.expr("mode in ('INSIDE', 'BOTH')")
_IS_OUTSIDE = Condition.expr("mode == 'OUTSIDE'")
_IS_BOTH = Condition.expr("mode == 'BOTH'")
_IS_INSIDE = Condition.expr("mode == 'INSIDE'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_audit=start_audit,
            record_context=record_context,
            source_review=source_review,
            blackbox_review=blackbox_review,
            infra_sweep=infra_sweep,
            rate_limit_deep_dive=rate_limit_deep_dive,
            write_advisory=write_advisory,
        )
        .with_transitions(
            ("start_audit", "record_context"),
            # After context, branch on mode.
            ("record_context", "source_review", _IS_INSIDE_OR_BOTH),
            ("record_context", "blackbox_review", _IS_OUTSIDE),
            # After source, BOTH continues to blackbox; INSIDE skips to infra.
            ("source_review", "blackbox_review", _IS_BOTH),
            ("source_review", "infra_sweep", _IS_INSIDE),
            # After blackbox, always infra (covers OUTSIDE direct and BOTH continuation).
            ("blackbox_review", "infra_sweep"),
            ("infra_sweep", "rate_limit_deep_dive"),
            ("rate_limit_deep_dive", "write_advisory"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            target="",
            mode="",
            authorization_source="",
            context_findings={},
            source_findings=[],
            blackbox_findings=[],
            infra_findings=[],
            rate_limit_findings=[],
            advisory=None,
            audit_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_audit")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="skill-security-audit",
        instructions=(
            "Web app security audit via a real Claude Code SKILL "
            "decomposed into an FSM of prompts. The CALLER LLM "
            "(whoever is driving you through MCP) does the thinking; "
            "this FSM just emits one structured prompt per phase and "
            "stores your responses. Start with "
            "start_audit(target, mode, authorization_source=None). "
            "Modes: INSIDE (you have filesystem access), OUTSIDE (URL "
            "only, scope-on-file required), BOTH (widest scope). "
            "Each subsequent step takes your findings from the prior "
            "phase as input and emits the next phase's prompt in "
            "state.current_prompt. Phases: context -> source review "
            "(INSIDE/BOTH) -> blackbox review (OUTSIDE/BOTH) -> infra "
            "sweep -> rate-limit deep-dive -> write advisory "
            "(terminal). Read burr://state after every step to see "
            "the current prompt; burr://history for the full audit "
            "trail. Source SKILL at examples/skills/security-audit/."
        ),
    )


if __name__ == "__main__":
    build_server().run()
