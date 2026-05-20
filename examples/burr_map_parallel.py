"""Multi-temperature sampling fan-out using Burr's native MapStates.

This is the Burr-native parallelism pattern. Where
``parallel_research.py`` fans out by calling ``asyncio.gather`` over
``spawn_subapp`` (BurrMCP's own primitive), this demo uses
``burr.core.parallelism.MapStates``, Burr's built-in map-reduce
action. Both work through BurrMCP unchanged; the point of this demo
is that Burr's own concurrency primitive comes along for free when
you ``mount`` an Application.

The scenario is multi-temperature sampling: the same scoring task is
fanned out across several "temperatures" (just synthetic noise
levels, no LLM involved), each producing a candidate output with a
deterministic score, and the reducer picks the highest-scored
candidate.

FSM shape:

    configure -> prepare_inputs -> map_and_reduce -> finalize

The ``map_and_reduce`` action is a ``MapStates`` subclass. It
generates one sub-state per temperature in ``state["temperatures"]``
and runs a single ``sample_candidate`` action against each sub-state
in parallel. The ``reduce`` method walks the per-task outputs, picks
the best score, and writes ``samples``, ``best_temperature``,
``best_output``, ``best_score`` back to the parent state.

Caveats / divergence from ``parallel_research``:

* MapStates spawns its own sub-Applications via the parent
  Application's tracker (``"cascade"``), not through
  ``burrmcp.spawn_subapp``. As a result, the per-task sub-runs do
  *not* appear under ``burr://subruns`` (that resource only lists
  spawn_subapp calls). They do show up on disk under the same
  ``LocalTrackingClient`` project directory as the parent run, as
  child app folders, so trace data is preserved, just not surfaced
  through BurrMCP's subruns resource. The reducer captures every
  per-task output into ``state["samples"]`` so callers can inspect
  the fan-out from the parent state directly.
* MapStates wraps a bare ``@action`` callable through
  ``RunnableGraph.create``; we do not pass the function in directly
  because Burr's MapStates assumes the action either is already an
  Action object or comes wrapped in a RunnableGraph.

Run as a stdio server:

    uv run python examples/burr_map_parallel.py

Try:

    configure(prompt="rank these bug reports")
    prepare_inputs()
    map_and_reduce()
    finalize()
"""

from __future__ import annotations

import hashlib
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.application import ApplicationContext
from burr.core.parallelism import MapStates, RunnableGraph
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "burr-map-parallel-demo"

# Default temperature grid. Picked to span the typical sampling range
# (zero-noise greedy -> high-noise exploratory) so the demo always has
# a clear winner.
_DEFAULT_TEMPERATURES: tuple[float, ...] = (0.0, 0.3, 0.7, 1.0)


# -- scoring primitives (pure, deterministic) -----------------------


def _synthesize_candidate(prompt: str, temperature: float) -> str:
    """Synthesize a deterministic candidate string for a given prompt
    and temperature. No LLM, no randomness: hash the inputs and slice
    the hex digest into a stable per-(prompt, temperature) blob. The
    point is that different temperatures produce visibly different
    candidates; the content itself is not meaningful.
    """
    digest = hashlib.sha256(f"{prompt}::{temperature}".encode()).hexdigest()
    return f"candidate(t={temperature}):{digest[:16]}"


def _score_candidate(prompt: str, temperature: float, candidate: str) -> float:
    """Deterministic score for a (prompt, temperature, candidate)
    triple. Designed so that the optimum is *not* at temperature 0.0
    (greedy) and *not* at temperature 1.0 (full-noise), so the
    reducer has to compare across temperatures rather than just
    picking an endpoint.

    The score is shaped as a downward parabola centred at 0.5 with
    a small per-prompt perturbation, plus a stable offset from the
    candidate's hash so two temperatures that happen to tie at the
    parabola get broken consistently.
    """
    # Parabolic envelope: peak at t=0.5, value 1.0; falls to 0.0 at
    # the endpoints t=0.0 and t=1.0.
    base = 1.0 - ((temperature - 0.5) * 2.0) ** 2
    # Tiny prompt-dependent perturbation so different prompts can have
    # different optima; small enough not to swamp the parabola.
    perturb = (int(hashlib.sha256(prompt.encode()).hexdigest(), 16) % 1000) / 100000.0
    # Hash-derived tie-breaker on the candidate itself, in [0, 0.001).
    tie = int(hashlib.sha256(candidate.encode()).hexdigest(), 16) % 1000 / 1_000_000.0
    return round(base + perturb + tie, 6)


# -- per-task action (what MapStates fans out) ----------------------


@action(reads=["prompt", "temperature"], writes=["candidate", "score"])
def sample_candidate(state: State) -> State:
    """Run one (prompt, temperature) sampling step.

    Synthesizes a deterministic candidate string and assigns it a
    score under ``_score_candidate``. This is the action that
    MapStates instantiates once per sub-state.
    """
    candidate = _synthesize_candidate(state["prompt"], state["temperature"])
    score = _score_candidate(state["prompt"], state["temperature"], candidate)
    return state.update(candidate=candidate, score=score)


# -- MapStates: the native Burr parallelism primitive ---------------


class MapAndReduce(MapStates):
    """Fans out ``sample_candidate`` across every temperature in
    ``state["temperatures"]`` and reduces by picking the best score.

    This is the same map-reduce pattern as the LLM example in the
    Burr docs (multiple prompts to one LLM action), reshaped for a
    non-LLM scoring task so the demo stays hermetic.
    """

    def action(self, state: State, inputs: dict[str, Any]):
        # Wrap the bare @action callable in a RunnableGraph so
        # MapStates' internal action() generator (which yields a
        # SubgraphType) gets an explicitly named single-node graph.
        return RunnableGraph.create(sample_candidate)

    def states(
        self,
        state: State,
        context: ApplicationContext,
        inputs: dict[str, Any],
    ):
        for temperature in state["temperatures"]:
            yield state.update(temperature=temperature)

    def reduce(self, state: State, states) -> State:
        samples: list[dict[str, Any]] = [
            {
                "temperature": sub_state["temperature"],
                "candidate": sub_state["candidate"],
                "score": sub_state["score"],
            }
            for sub_state in states
        ]
        if not samples:
            return state.update(
                samples=[],
                best_temperature=None,
                best_candidate=None,
                best_score=None,
            )
        # Sort by score descending, then by temperature ascending so
        # ties resolve deterministically.
        samples_sorted = sorted(samples, key=lambda s: (-s["score"], s["temperature"]))
        best = samples_sorted[0]
        return state.update(
            samples=samples_sorted,
            best_temperature=best["temperature"],
            best_candidate=best["candidate"],
            best_score=best["score"],
        )

    @property
    def reads(self) -> list[str]:
        return ["prompt", "temperatures"]

    @property
    def writes(self) -> list[str]:
        return [
            "samples",
            "best_temperature",
            "best_candidate",
            "best_score",
        ]


# -- parent actions --------------------------------------------------


@action(
    reads=[],
    writes=["prompt", "temperatures", "status"],
)
def configure(
    state: State,
    prompt: str = "rank these bug reports",
    temperatures: list[float] | None = None,
) -> State:
    """Set the prompt and the temperature grid to fan out across.

    Args:
        prompt: Freeform prompt the per-task action receives.
        temperatures: Optional list of temperatures in ``[0.0, 1.0]``.
            Defaults to ``(0.0, 0.3, 0.7, 1.0)``. Must be non-empty
            and contain unique values inside ``[0.0, 1.0]``.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    grid = list(temperatures) if temperatures is not None else list(_DEFAULT_TEMPERATURES)
    if not grid:
        raise ValueError("temperatures must be a non-empty list")
    if len(set(grid)) != len(grid):
        raise ValueError(f"temperatures must be unique, got: {grid}")
    for t in grid:
        if not isinstance(t, int | float):
            raise ValueError(f"temperatures must be numeric, got: {t!r}")
        if not (0.0 <= float(t) <= 1.0):
            raise ValueError(f"temperatures must lie in [0.0, 1.0], got: {t}")
    return state.update(
        prompt=prompt,
        temperatures=[float(t) for t in grid],
        status="configured",
    )


@action(
    reads=["prompt", "temperatures", "status"],
    writes=["fanout_plan", "status"],
)
def prepare_inputs(state: State) -> State:
    """Record the planned fan-out before MapStates runs.

    Builds the per-task input descriptions that ``MapAndReduce``
    will iterate over. The actual fan-out happens inside the
    ``MapStates.states`` generator; this step makes the plan
    visible in ``burr://history`` so an inspecting client can see
    what was *about* to be parallelised before it ran.
    """
    if state["status"] != "configured":
        raise RuntimeError(f"prepare_inputs requires status=='configured', got {state['status']!r}")
    plan = [{"prompt": state["prompt"], "temperature": t} for t in state["temperatures"]]
    return state.update(fanout_plan=plan, status="prepared")


@action(
    reads=["samples", "best_temperature", "best_candidate", "best_score", "status"],
    writes=["final_report", "status"],
)
def finalize(state: State) -> State:
    """Terminal: assemble a small report from the reduced result."""
    if state["status"] != "reduced":
        raise RuntimeError(f"finalize requires status=='reduced', got {state['status']!r}")
    report: dict[str, Any] = {
        "best_temperature": state["best_temperature"],
        "best_candidate": state["best_candidate"],
        "best_score": state["best_score"],
        "n_samples": len(state["samples"]),
        "samples": state["samples"],
    }
    return state.update(final_report=report, status="done")


# Wrapper around the MapStates instance so we can flip status to
# "reduced" after the reducer runs. MapStates' reduce() returns the
# new state but we want one more hop to record the status transition
# explicitly in burr://history. Simpler: subclass MapAndReduce and
# override reduce to also write status.


class MapAndReduceWithStatus(MapAndReduce):
    """MapAndReduce that also flips ``status`` to ``"reduced"`` so
    the downstream ``finalize`` transition gate fires."""

    def reduce(self, state: State, states) -> State:
        new_state = super().reduce(state, states)
        return new_state.update(status="reduced")

    @property
    def writes(self) -> list[str]:
        return [*super().writes, "status"]


# -- application -----------------------------------------------------


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            configure=configure,
            prepare_inputs=prepare_inputs,
            map_and_reduce=MapAndReduceWithStatus(),
            finalize=finalize,
        )
        .with_transitions(
            ("configure", "prepare_inputs"),
            ("prepare_inputs", "map_and_reduce"),
            ("map_and_reduce", "finalize"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            prompt=None,
            temperatures=[],
            fanout_plan=[],
            samples=[],
            best_temperature=None,
            best_candidate=None,
            best_score=None,
            final_report=None,
            status="initial",
        )
        .with_entrypoint("configure")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="burr-map-parallel",
        instructions=(
            "Multi-temperature sampling fan-out using Burr's native "
            "MapStates primitive. Walk: configure(prompt, "
            "temperatures) -> prepare_inputs -> map_and_reduce -> "
            "finalize. The map_and_reduce step is a MapStates "
            "action that runs sample_candidate(prompt, temperature) "
            "across every temperature in state[temperatures] and "
            "reduces by best score. Note: MapStates sub-runs use "
            "Burr's own tracker cascade, not burrmcp.spawn_subapp, "
            "so they do not surface under burr://subruns. The "
            "per-task outputs are recorded in state[samples] so "
            "callers can inspect the full fan-out from parent state."
        ),
    )


# Ray executor variant
# ====================
#
# Burr's MapStates dispatches per-task work through whatever executor
# `ApplicationBuilder.with_parallel_executor(...)` is configured with.
# The default is a thread-pool executor, which is what this demo uses.
# Burr also ships `burr.integrations.ray.RayExecutor` (a thin shim that
# wraps `concurrent.futures.Executor` around `ray.remote()`), so the
# exact same MapStates action can fan out across a Ray cluster (or
# Ray's single-process local mode) with one factory line changed.
#
# Sketch:
#
#     import ray
#     from burr.integrations.ray import RayExecutor
#
#     def build_application_ray():
#         ray.init(ignore_reinit_error=True)
#         return (
#             ApplicationBuilder()
#             .with_actions(
#                 configure=configure,
#                 prepare_inputs=prepare_inputs,
#                 map_and_reduce=MapAndReduceWithStatus(),
#                 finalize=finalize,
#             )
#             .with_parallel_executor(RayExecutor)  # <- the swap
#             .with_transitions(...)
#             .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
#             .with_state(...)
#             .with_entrypoint("configure")
#             .build()
#         )
#
# `mount(build_application_ray, ...)` then serves the same MCP surface
# (four tools in STEP mode) with the per-task fan-out happening through
# Ray. No adapter changes. This demo ships with the thread-pool default
# to keep `uv sync` light; `ray` is intentionally not a project dep.


if __name__ == "__main__":
    build_server().run()
