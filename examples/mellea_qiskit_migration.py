"""Mellea inside a Burr graph: Qiskit code-migration repair loop.

A real Mellea sample, mirrored. Original at
``docs/examples/instruct_validate_repair/qiskit_code_validation/``
in https://github.com/generative-computing/mellea. The original
demo uses a ``flake8-qiskit-migration`` plugin as the validator;
we inline a small set of the same deprecation patterns drawn from
Qiskit's own 1.0 migration guide so this demo runs without any
extra plugin.

The shape: hand the FSM a chunk of pre-Qiskit-1.0 code that uses
removed APIs (``IBMQ.load_account()``, ``execute(circuit, backend)``,
``Aer.get_backend(...)``, ``QasmSimulator()``); a single Burr
action calls Mellea, which internally runs its
generate-validate-repair loop against the migration checker, and
returns the cleaned-up code plus a per-sample validation trace.
The audit-trail-of-attempts is the demo's payoff.

What the FSM owns: the workflow around the Mellea call (input,
routing on success/failure, terminal report) plus the audit trail
visible in ``burr://history`` and ``burr://state``.

What Mellea owns: the instruct-validate-repair loop inside the
single ``mellea_repair_loop`` action.

FSM shape:

    accept_problem(deprecated_code, max_repair_rounds)
      -> mellea_repair_loop
        -> finalize_success      (migration_check returns no issues)
        -> finalize_giveup       (Mellea exhausted its loop budget)

Pre-req for real-world runs:

    pip install mellea
    ollama serve &
    ollama pull granite4:micro

Tests monkey-patch ``_call_mellea`` so they run hermetically.

Run:

    python examples/mellea_qiskit_migration.py
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "mellea-qiskit-migration-demo"
_DEFAULT_LOOP_BUDGET = 3


class MelleaUnavailable(RuntimeError):
    """Raised when Mellea isn't installed in the active environment."""


# ── deterministic Qiskit-migration checker ─────────────────────────


# Each pattern is a (compiled regex, human-readable issue, fix hint)
# triple. Drawn from Qiskit's 1.0 migration guide. Adding patterns
# is one line each; the validator stays a pure function.
_MIGRATION_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\bfrom\s+qiskit\s+import\s+.*\bIBMQ\b"),
        "IBMQ is removed in Qiskit 1.x.",
        (
            "Replace `from qiskit import IBMQ` with "
            "`from qiskit_ibm_runtime import QiskitRuntimeService`."
        ),
    ),
    (
        re.compile(r"\bIBMQ\.load_account\s*\("),
        "IBMQ.load_account() is removed.",
        (
            "Use `service = QiskitRuntimeService()` after saving credentials "
            "with `QiskitRuntimeService.save_account(...)`."
        ),
    ),
    (
        re.compile(r"\bIBMQ\.providers?\s*\("),
        "IBMQ.providers() / IBMQ.provider() is removed.",
        "Use `service.backends()` from a QiskitRuntimeService instance.",
    ),
    (
        re.compile(r"\bexecute\s*\(\s*\w+\s*,\s*\w+"),
        "qiskit.execute(circuit, backend, ...) is removed.",
        "Use `backend.run(circuit, ...)` or, for primitives, `SamplerV2(backend).run([circuit])`.",
    ),
    (
        re.compile(r"\bAer\.get_backend\s*\("),
        "Aer.get_backend(...) is moved out of qiskit core.",
        "Use `from qiskit_aer import AerSimulator; sim = AerSimulator()` instead.",
    ),
    (
        re.compile(r"\bQasmSimulator\s*\("),
        "QasmSimulator() is replaced by AerSimulator() from qiskit_aer.",
        "Use `from qiskit_aer import AerSimulator; sim = AerSimulator()`.",
    ),
]


def check_qiskit_migration(code: str) -> list[dict[str, str]]:
    """Return a list of deprecated-API issues remaining in ``code``.

    Empty list means the code passes. Each issue dict has
    ``pattern`` (a human description of what was matched),
    ``issue`` (the deprecation summary), and ``fix`` (the
    one-line migration hint from the Qiskit 1.0 guide).
    """
    findings: list[dict[str, str]] = []
    for pattern, issue, fix in _MIGRATION_PATTERNS:
        if pattern.search(code):
            findings.append({"pattern": pattern.pattern, "issue": issue, "fix": fix})
    return findings


# ── Mellea wrapper (lazy-imported) ─────────────────────────────────


async def _call_mellea(
    deprecated_code: str,
    loop_budget: int = _DEFAULT_LOOP_BUDGET,
) -> dict[str, Any]:
    """Run one Mellea instruct-validate-repair pass on the code.

    Lazy-imports Mellea so this module is importable without Mellea
    installed (tests monkey-patch this function). The returned shape
    is normalised so the action body doesn't depend on Mellea's
    internal class names.
    """
    try:
        import mellea
        from mellea.stdlib.requirement import req, simple_validate
        from mellea.stdlib.sampling import RepairTemplateStrategy
    except ImportError as e:
        raise MelleaUnavailable(
            "Mellea is not installed. Install it with `pip install mellea` "
            "and pull the default Granite model with `ollama pull granite4:micro`. "
            "See https://github.com/generative-computing/mellea for details."
        ) from e

    def _passes_migration(candidate: str) -> bool:
        return len(check_qiskit_migration(candidate)) == 0

    def _no_commentary(candidate: str) -> bool:
        # Refuse code blocks that lead with prose like "Here's the migrated code:"
        return "```" not in candidate and not re.match(
            r"^\s*(here\b|sure\b|the\b)", candidate, re.IGNORECASE
        )

    session = mellea.start_session()
    result = session.instruct(
        (
            "Migrate this Qiskit code to the modern Qiskit 1.x API. "
            "Output only the migrated Python code, nothing else.\n\n"
            f"```python\n{deprecated_code}\n```"
        ),
        requirements=[
            req(
                "Output only Python code, no commentary or code fences.",
                validation_fn=simple_validate(_no_commentary),
            ),
            req(
                "Code uses no removed Qiskit 1.x APIs (no IBMQ, no execute(), no QasmSimulator).",
                validation_fn=simple_validate(_passes_migration),
            ),
        ],
        strategy=RepairTemplateStrategy(loop_budget=loop_budget),
        return_sampling_results=True,
    )

    try:
        samples = list(result.sample_generations)
        validations = list(result.sample_validations)
        success = bool(result.success)
    except AttributeError:
        # Mellea returned a plain string (rare; older API shape). Treat as a
        # single successful sample with no validation trace.
        return {
            "repaired_code": str(result),
            "success": True,
            "samples_tried": 1,
            "validation_trace": [],
        }

    chosen_idx = len(samples) - 1
    if success:
        for i, vlist in enumerate(validations):
            if all(getattr(v, "success", True) for v in vlist):
                chosen_idx = i
                break
    chosen_code = str(samples[chosen_idx]) if samples else ""
    # Per-attempt audit log: which requirements passed/failed each round.
    trace: list[dict[str, Any]] = []
    for round_idx, vlist in enumerate(validations):
        trace.append(
            {
                "round": round_idx + 1,
                "checks": [
                    {
                        "name": str(getattr(v, "requirement", f"check_{ci}")),
                        "passed": bool(getattr(v, "success", True)),
                        "reason": str(getattr(v, "reason", "")),
                    }
                    for ci, v in enumerate(vlist)
                ],
            }
        )
    return {
        "repaired_code": chosen_code,
        "success": success,
        "samples_tried": len(samples),
        "validation_trace": trace,
    }


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "deprecated_code",
        "max_repair_rounds",
        "repaired_code",
        "validation_trace",
        "remaining_issues",
        "samples_tried",
        "success",
        "final_report",
        "started_at",
        "log",
    ],
)
async def accept_problem(
    state: State,
    deprecated_code: str,
    max_repair_rounds: int = _DEFAULT_LOOP_BUDGET,
) -> State:
    """Receive the pre-Qiskit-1.x code to migrate. Validates inputs and
    runs the deterministic migration check on the input so the report
    can show how many issues the LLM started with.
    """
    if not deprecated_code.strip():
        raise ValueError("deprecated_code must not be empty")
    if max_repair_rounds < 1:
        raise ValueError("max_repair_rounds must be >= 1")
    starting_issues = check_qiskit_migration(deprecated_code)
    return state.update(
        deprecated_code=deprecated_code,
        max_repair_rounds=max_repair_rounds,
        repaired_code=None,
        validation_trace=[],
        remaining_issues=[],
        samples_tried=0,
        success=False,
        final_report=None,
        started_at=datetime.now(UTC).isoformat(),
        log=[
            f"Problem accepted: {len(starting_issues)} deprecated pattern(s) detected upfront, "
            f"max_repair_rounds={max_repair_rounds}"
        ],
    )


@action(
    reads=["deprecated_code", "max_repair_rounds", "log"],
    writes=[
        "repaired_code",
        "validation_trace",
        "remaining_issues",
        "samples_tried",
        "success",
        "log",
    ],
)
async def mellea_repair_loop(state: State) -> State:
    """The Mellea node. Calls Mellea's instruct-validate-repair primitive
    with the Qiskit migration checker as a validation_fn. Stashes the
    chosen sample plus the per-attempt validation log into state.
    """
    result = await _call_mellea(
        state["deprecated_code"],
        loop_budget=state["max_repair_rounds"],
    )
    # Re-run our deterministic check on whatever Mellea chose, since
    # Mellea's success flag is based on its own internal scoring and
    # we want the canonical "did this actually pass the linter" answer.
    remaining = check_qiskit_migration(result["repaired_code"])
    canonical_success = len(remaining) == 0
    return state.update(
        repaired_code=result["repaired_code"],
        validation_trace=result["validation_trace"],
        remaining_issues=remaining,
        samples_tried=result["samples_tried"],
        success=canonical_success,
        log=[
            *state["log"],
            f"Mellea returned after {result['samples_tried']} sample(s); "
            f"{len(remaining)} deprecated pattern(s) remaining; success={canonical_success}",
        ],
    )


@action(
    reads=[
        "deprecated_code",
        "repaired_code",
        "validation_trace",
        "samples_tried",
        "started_at",
        "log",
    ],
    writes=["final_report", "log"],
)
async def finalize_success(state: State) -> State:
    """Terminal: every deprecated pattern was resolved."""
    return state.update(
        final_report={
            "status": "migrated",
            "started_at": state["started_at"],
            "ended_at": datetime.now(UTC).isoformat(),
            "samples_tried": state["samples_tried"],
            "original_code": state["deprecated_code"],
            "migrated_code": state["repaired_code"],
            "validation_trace": state["validation_trace"],
        },
        log=[
            *state["log"],
            f"Migration complete in {state['samples_tried']} sample(s).",
        ],
    )


@action(
    reads=[
        "deprecated_code",
        "repaired_code",
        "validation_trace",
        "remaining_issues",
        "samples_tried",
        "started_at",
        "log",
    ],
    writes=["final_report", "log"],
)
async def finalize_giveup(state: State) -> State:
    """Terminal: Mellea exhausted its loop budget; remaining issues are
    reported so the human can finish the migration by hand."""
    return state.update(
        final_report={
            "status": "needs_human",
            "started_at": state["started_at"],
            "ended_at": datetime.now(UTC).isoformat(),
            "samples_tried": state["samples_tried"],
            "original_code": state["deprecated_code"],
            "best_attempt": state["repaired_code"],
            "remaining_issues": state["remaining_issues"],
            "validation_trace": state["validation_trace"],
        },
        log=[
            *state["log"],
            f"Mellea could not fully migrate after {state['samples_tried']} sample(s); "
            f"{len(state['remaining_issues'])} pattern(s) still need fixing.",
        ],
    )


# ── graph ──────────────────────────────────────────────────────────


_SUCCESS = Condition.expr("success == True")
_GIVEUP = Condition.expr("success == False")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            accept_problem=accept_problem,
            mellea_repair_loop=mellea_repair_loop,
            finalize_success=finalize_success,
            finalize_giveup=finalize_giveup,
        )
        .with_transitions(
            ("accept_problem", "mellea_repair_loop"),
            ("mellea_repair_loop", "finalize_success", _SUCCESS),
            ("mellea_repair_loop", "finalize_giveup", _GIVEUP),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            deprecated_code="",
            max_repair_rounds=_DEFAULT_LOOP_BUDGET,
            repaired_code=None,
            validation_trace=[],
            remaining_issues=[],
            samples_tried=0,
            success=False,
            final_report=None,
            started_at=None,
            log=[],
        )
        .with_entrypoint("accept_problem")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="mellea-qiskit-migration",
        instructions=(
            "Mellea-inside-a-Burr-node demo: Qiskit code migration via "
            "instruct-validate-repair. Hand it a chunk of pre-Qiskit-1.0 "
            "code (uses IBMQ.load_account(), execute(circuit, backend), "
            "QasmSimulator(), Aer.get_backend(...), etc.) via "
            "accept_problem(deprecated_code, max_repair_rounds=3). The "
            "mellea_repair_loop action calls Mellea's session.instruct "
            "with the Qiskit migration patterns as a deterministic "
            "validation_fn; Mellea runs its internal generate-validate-"
            "repair loop and returns the chosen sample plus a "
            "per-attempt validation log. The FSM then routes to "
            "finalize_success or finalize_giveup based on whether every "
            "deprecated pattern was resolved. The migrated code, the "
            "per-attempt audit trace, and any remaining issues all live "
            "in burr://state and burr://history. Pre-req: pip install "
            "mellea, ollama serve, ollama pull granite4:micro."
        ),
    )


if __name__ == "__main__":
    build_server().run()
