"""User-defined Burr lifecycle hooks, plugged through `mount()` unchanged.

Domain: a tiny ETL-shaped pipeline (ingest -> enrich -> aggregate ->
finalize), instrumented with two user-written hooks:

* ``TimingHook``: wall-clock timer per action. Records every duration
  by action name and exposes a snapshot.
* ``StepCounter``: post-step counter, demonstrates multi-hook
  composition (Burr applies hooks in registration order).

The hooks attach via ``ApplicationBuilder.with_hooks(...)``. BurrMCP's
``mount()`` doesn't have to know they're there: the hooks fire at
Burr's action-execution layer, whether the action runs via MCP
``step``, ``app.astep`` from a script, or ``app.run(halt_after=...)``.
That's the 1:1 surface point: any user code plugged into Burr's hook
system comes along through ``mount()`` for free.

The hook data is exposed back to the MCP client via a custom
``burr://timings`` resource added on the mounted FastMCP server.
This shows that users can register their own resources on top of
the ones BurrMCP ships, which is occasionally useful for hook-driven
side channels like this one.

Run:

    uv run python examples/pipeline_hooks.py
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.lifecycle import PostRunStepHook, PreRunStepHook
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "pipeline-hooks-demo"


# == hooks ===========================================================


class TimingHook(PreRunStepHook, PostRunStepHook):
    """Per-action wall-clock timer.

    Subclasses both ``PreRunStepHook`` and ``PostRunStepHook``. Burr
    fires ``pre_run_step`` just before an action's body runs and
    ``post_run_step`` just after (with ``exception`` populated when
    the action raised). The hook keeps a per-action history of
    durations on a regular Python dict; ``snapshot()`` summarises it.
    """

    def __init__(self) -> None:
        self._starts: dict[tuple[str, int], float] = {}
        self.durations_ms: dict[str, list[float]] = defaultdict(list)

    def pre_run_step(self, *, app_id: str, sequence_id: int, **_: Any) -> None:
        self._starts[(app_id, sequence_id)] = time.perf_counter()

    def post_run_step(
        self,
        *,
        app_id: str,
        sequence_id: int,
        action: Any,
        exception: Exception | None,
        **_: Any,
    ) -> None:
        start = self._starts.pop((app_id, sequence_id), None)
        if start is None:
            return
        ms = (time.perf_counter() - start) * 1000
        self.durations_ms[action.name].append(ms)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, samples in self.durations_ms.items():
            out[name] = {
                "runs": len(samples),
                "total_ms": round(sum(samples), 3),
                "avg_ms": round(sum(samples) / len(samples), 3) if samples else 0.0,
            }
        return out


class StepCounter(PostRunStepHook):
    """Counts successful steps. Demonstrates multi-hook composition."""

    def __init__(self) -> None:
        self.total_steps = 0
        self.errors = 0

    def post_run_step(
        self,
        *,
        action: Any,
        exception: Exception | None,
        **_: Any,
    ) -> None:
        if exception is None:
            self.total_steps += 1
        else:
            self.errors += 1


# == actions =========================================================


@action(reads=[], writes=["batch", "stage"])
def ingest(state: State, batch_size: int = 10) -> State:
    """Synthesise a batch of events."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1; got {batch_size}")
    events = [{"id": i, "value": (i * 7) % 13} for i in range(batch_size)]
    return state.update(batch=events, stage="ingested")


@action(reads=["batch"], writes=["enriched", "stage"])
def enrich(state: State) -> State:
    """Tag each event with a synthetic ``category`` field."""
    enriched = [{**ev, "category": "high" if ev["value"] > 6 else "low"} for ev in state["batch"]]
    return state.update(enriched=enriched, stage="enriched")


@action(reads=["enriched"], writes=["summary", "stage"])
def aggregate(state: State) -> State:
    """Sum values per category."""
    by_cat: dict[str, int] = {}
    for ev in state["enriched"]:
        cat = ev["category"]
        by_cat[cat] = by_cat.get(cat, 0) + ev["value"]
    return state.update(summary=by_cat, stage="aggregated")


@action(reads=["summary"], writes=["stage"])
def finalize(state: State) -> State:
    """Mark the pipeline complete. Terminal."""
    return state.update(stage="done")


# == graph ===========================================================


def build_application(
    *,
    timing_hook: TimingHook | None = None,
    counter_hook: StepCounter | None = None,
):
    """Build the pipeline Application, optionally instrumented.

    Pass ``timing_hook`` / ``counter_hook`` (either or both) to wire
    hooks via ``ApplicationBuilder.with_hooks(...)``. Tests create
    fresh hooks per Application; ``build_server`` shares one set
    across all sessions so the ``burr://timings`` resource sees the
    cumulative picture.
    """
    hooks = [h for h in (timing_hook, counter_hook) if h is not None]
    builder = (
        ApplicationBuilder()
        .with_actions(
            ingest=ingest,
            enrich=enrich,
            aggregate=aggregate,
            finalize=finalize,
        )
        .with_transitions(
            ("ingest", "enrich"),
            ("enrich", "aggregate"),
            ("aggregate", "finalize"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(batch=[], enriched=[], summary={}, stage="new")
        .with_entrypoint("ingest")
    )
    if hooks:
        builder = builder.with_hooks(*hooks)
    return builder.build()


def build_server():
    """Mount the pipeline with server-scoped hooks.

    Hooks are created once at server build time and shared across all
    MCP sessions hitting this server; the ``burr://timings`` resource
    therefore returns cumulative data across every session that has
    run. A real deployment that wants per-session timings would
    construct fresh hooks inside the factory closure instead.
    """
    timing = TimingHook()
    counter = StepCounter()

    def factory():
        return build_application(timing_hook=timing, counter_hook=counter)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="pipeline-hooks",
        instructions=(
            "ETL-shaped pipeline (ingest -> enrich -> aggregate -> "
            "finalize) instrumented with two user-defined Burr "
            "lifecycle hooks: TimingHook (wall-clock per action) and "
            "StepCounter (post-step success/error counts). The hooks "
            "fire automatically on every step. Read burr://timings "
            "for the captured timing snapshot."
        ),
    )

    @server.resource("burr://timings")
    async def _timings_resource() -> str:
        """Cumulative hook-recorded data across every session."""
        return json.dumps(
            {
                "total_steps": counter.total_steps,
                "errors": counter.errors,
                "by_action": timing.snapshot(),
            },
            indent=2,
        )

    return server


if __name__ == "__main__":
    build_server().run()
