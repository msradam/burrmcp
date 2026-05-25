"""Class-based Burr Action: the escape hatch from ``@action``.

When the ``@action`` decorator fits, use it; 95% of the time it does.
But the decorator wraps a *function*, and that function's behavior is
fixed at decoration time. When you need ACTION-INSTANCE-LEVEL
configuration -- multiple actions sharing implementation but
parameterised differently at construction -- subclass ``burr.core.Action``
directly.

This demo's class-based action is ``QualityCheckAction``, configured
at ``__init__`` with a list of ``(rule_name, predicate)`` pairs. Each
instance becomes a separate action in the FSM; the FSM uses two
instances with different rule sets to validate the same payload at
different stages.

Shape:

    ingest -> shallow_check -> deep_check -> finalize
                  |                |
                  └─ shallow rules ─┴─ deep rules

Both ``shallow_check`` and ``deep_check`` are instances of the same
``QualityCheckAction`` class. The class itself is generic; the rule
list is the per-instance configuration. This is exactly what you
can't do cleanly with the ``@action`` decorator.

Run:

    uv run python examples/class_action.py
"""

from __future__ import annotations

from collections.abc import Callable

from burr.core import Action, ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "class-action-demo"


# == the class-based action ==========================================


# A rule takes the current state and returns None if the check passes
# or a short violation string if it fails.
Rule = Callable[[State], str | None]


class QualityCheckAction(Action):
    """Run a list of rules over the current state.

    Reads:  ``batch`` (whatever the rules look at)
    Writes: the per-instance ``violations_key`` (e.g.
            ``shallow_violations`` or ``deep_violations``) so two
            instances don't collide.

    Each rule returns either ``None`` (passed) or a short violation
    string. The action accumulates the violations and writes the
    list. The action body never raises on a violation; it just
    records, so the caller LLM (or the FSM's branch logic) can
    decide what to do.
    """

    def __init__(self, rules: list[tuple[str, Rule]], *, violations_key: str) -> None:
        super().__init__()
        self._rules = rules
        self._violations_key = violations_key

    @property
    def reads(self) -> list[str]:
        return ["batch"]

    @property
    def writes(self) -> list[str]:
        return [self._violations_key]

    @property
    def inputs(self) -> list[str]:
        # No runtime inputs: the rules are fixed at construction.
        return []

    def run(self, state: State, **_: object) -> dict:
        violations = []
        for rule_name, predicate in self._rules:
            verdict = predicate(state)
            if verdict is not None:
                violations.append(f"{rule_name}: {verdict}")
        return {"violations": violations}

    def update(self, result: dict, state: State) -> State:
        return state.update(**{self._violations_key: result["violations"]})


# == rules ==========================================================


def _no_empty_batch(state: State) -> str | None:
    if not state["batch"]:
        return "batch is empty"
    return None


def _all_have_id(state: State) -> str | None:
    bad = [i for i, ev in enumerate(state["batch"]) if "id" not in ev]
    if bad:
        return f"events missing id at indices {bad[:3]}"
    return None


def _values_in_range(state: State) -> str | None:
    bad = [ev["id"] for ev in state["batch"] if not (0 <= ev.get("value", -1) <= 100)]
    if bad:
        return f"out-of-range values for ids {bad[:3]}"
    return None


def _no_duplicate_ids(state: State) -> str | None:
    ids = [ev.get("id") for ev in state["batch"]]
    if len(ids) != len(set(ids)):
        return "duplicate ids present"
    return None


# Two distinct rule sets; same QualityCheckAction class, two instances.
_SHALLOW_RULES = [
    ("non_empty", _no_empty_batch),
    ("has_id", _all_have_id),
]
_DEEP_RULES = [
    ("non_empty", _no_empty_batch),
    ("has_id", _all_have_id),
    ("value_in_range", _values_in_range),
    ("no_duplicates", _no_duplicate_ids),
]


# == surrounding @action decorators (the 95% case) ==================


@action(reads=[], writes=["batch", "stage"])
def ingest(state: State, batch: list[dict]) -> State:
    """Place a batch of events on state."""
    return state.update(batch=list(batch), stage="ingested")


@action(
    reads=["shallow_violations", "deep_violations", "batch"],
    writes=["stage", "verdict"],
)
def finalize(state: State) -> State:
    """Combine the two violation lists into a final verdict."""
    total = len(state["shallow_violations"]) + len(state["deep_violations"])
    verdict = "clean" if total == 0 else f"flagged ({total} violation(s))"
    return state.update(stage="finalized", verdict=verdict)


# == graph ===========================================================


def build_application():
    """Wire two instances of the class-based action into one FSM."""
    shallow_check = QualityCheckAction(_SHALLOW_RULES, violations_key="shallow_violations")
    deep_check = QualityCheckAction(_DEEP_RULES, violations_key="deep_violations")
    return (
        ApplicationBuilder()
        .with_actions(
            ingest=ingest,
            shallow_check=shallow_check,
            deep_check=deep_check,
            finalize=finalize,
        )
        .with_transitions(
            ("ingest", "shallow_check"),
            ("shallow_check", "deep_check"),
            ("deep_check", "finalize"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            batch=[],
            shallow_violations=[],
            deep_violations=[],
            verdict=None,
            stage="new",
        )
        .with_entrypoint("ingest")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="class-action",
        instructions=(
            "Data-quality FSM where the two check actions "
            "(shallow_check, deep_check) are instances of the same "
            "class-based QualityCheckAction with different rule lists. "
            "Walk: ingest(batch) -> shallow_check -> deep_check -> "
            "finalize. Each check writes its own violations list "
            "(shallow_violations / deep_violations); finalize "
            "summarises both into state.verdict."
        ),
    )


if __name__ == "__main__":
    build_server().run()
