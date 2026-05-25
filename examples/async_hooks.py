"""Async + lifecycle-envelope Burr hooks.

Companion to ``pipeline_hooks.py``. That demo used the sync hook
subclasses; this one wires the async + application-level variants:

* ``PreRunStepHookAsync`` / ``PostRunStepHookAsync``: async-friendly
  per-step hooks. Useful when the hook itself wants to ``await``
  (post metrics to a remote service, write to an async logger, etc.).
* ``PostApplicationCreateHook``: fires once per Application build.
  Useful for recording session boot events.
* ``PreRunExecuteCallHookAsync`` / ``PostRunExecuteCallHookAsync``:
  fire on every Application execute boundary (``step`` / ``astep``
  / ``iterate`` / ``aiterate`` / ``run`` / ``arun`` / stream
  variants). Theodosia's adapter calls ``app.astep`` per MCP step,
  so the ``executes_started`` counter increments once per MCP step
  call. Useful for whole-execute-call accounting at a level above
  per-action timing.

Domain: a simulated async pipeline with ``asyncio.sleep`` standing in
for real I/O. Each action awaits a configurable per-action latency
so the timing hook captures something meaningful.

Run:

    uv run python examples/async_hooks.py
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.lifecycle import (
    PostApplicationCreateHook,
    PostApplicationExecuteCallHookAsync,
    PostRunStepHookAsync,
    PreApplicationExecuteCallHookAsync,
    PreRunStepHookAsync,
)
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "async-hooks-demo"


# == async hooks =====================================================


class AsyncTimingHook(PreRunStepHookAsync, PostRunStepHookAsync):
    """Async timing per action. ``pre_run_step`` and ``post_run_step``
    are coroutines, so Burr ``await``s them around each action body."""

    def __init__(self) -> None:
        self._starts: dict[tuple[str, int], float] = {}
        self.durations_ms: dict[str, list[float]] = defaultdict(list)

    async def pre_run_step(self, *, app_id: str, sequence_id: int, **_: Any) -> None:
        self._starts[(app_id, sequence_id)] = time.perf_counter()

    async def post_run_step(
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
        return {
            name: {
                "runs": len(samples),
                "total_ms": round(sum(samples), 3),
                "avg_ms": round(sum(samples) / len(samples), 3) if samples else 0.0,
            }
            for name, samples in self.durations_ms.items()
        }


class AppCreatedHook(PostApplicationCreateHook):
    """Fires once per Application build. Counts sessions opened."""

    def __init__(self) -> None:
        self.applications_created = 0

    def post_application_create(self, *, app_id: str, **_: Any) -> None:
        self.applications_created += 1


class AsyncExecuteCallHook(PreApplicationExecuteCallHookAsync, PostApplicationExecuteCallHookAsync):
    """Counts execute-call boundaries.

    Burr wraps every Application execute method (``step``, ``astep``,
    ``iterate``, ``aiterate``, ``run``, ``arun``, and the stream
    variants) with these hooks. Theodosia's adapter calls ``app.astep``
    once per MCP step, so each MCP step increments the counter by 1.
    Multi-action invocations like ``app.run`` increment the counter
    once for the whole call.
    """

    def __init__(self) -> None:
        self.executes_started = 0
        self.executes_completed = 0

    async def pre_run_execute_call(self, **_: Any) -> None:
        self.executes_started += 1

    async def post_run_execute_call(self, **_: Any) -> None:
        self.executes_completed += 1


# == async actions ===================================================


@action(reads=[], writes=["items", "stage"])
async def fetch(state: State, count: int = 5, latency_ms: int = 20) -> State:
    """Simulate fetching ``count`` items from a remote service."""
    await asyncio.sleep(latency_ms / 1000)
    items = [{"id": i, "weight": (i * 11) % 7 + 1} for i in range(count)]
    return state.update(items=items, stage="fetched")


@action(reads=["items"], writes=["scored", "stage"])
async def score(state: State, latency_ms: int = 30) -> State:
    """Simulate scoring each item via a remote ML service."""
    await asyncio.sleep(latency_ms / 1000)
    scored = [{**item, "score": item["weight"] * 1.5} for item in state["items"]]
    return state.update(scored=scored, stage="scored")


@action(reads=["scored"], writes=["best", "stage"])
async def pick_best(state: State) -> State:
    """Pick the highest-scoring item."""
    best = max(state["scored"], key=lambda item: item["score"])
    return state.update(best=best, stage="done")


# == graph ===========================================================


def build_application(
    *,
    timing_hook: AsyncTimingHook | None = None,
    app_create_hook: AppCreatedHook | None = None,
    execute_call_hook: AsyncExecuteCallHook | None = None,
):
    hooks = [h for h in (timing_hook, app_create_hook, execute_call_hook) if h is not None]
    builder = (
        ApplicationBuilder()
        .with_actions(fetch=fetch, score=score, pick_best=pick_best)
        .with_transitions(("fetch", "score"), ("score", "pick_best"))
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(items=[], scored=[], best=None, stage="new")
        .with_entrypoint("fetch")
    )
    if hooks:
        builder = builder.with_hooks(*hooks)
    return builder.build()


def build_server():
    timing = AsyncTimingHook()
    app_create = AppCreatedHook()
    execute = AsyncExecuteCallHook()

    def factory():
        return build_application(
            timing_hook=timing,
            app_create_hook=app_create,
            execute_call_hook=execute,
        )

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="async-hooks",
        instructions=(
            "Async pipeline (fetch -> score -> pick_best) instrumented "
            "with async + lifecycle-envelope Burr hooks: AsyncTimingHook "
            "(awaitable pre/post per step), AppCreatedHook (counts "
            "Application builds per session), AsyncExecuteCallHook "
            "(counters around every execute-call boundary, including "
            "each MCP step). Read theodosia://hooks for the snapshot."
        ),
    )

    @server.resource("theodosia://hooks")
    async def _hooks_resource() -> str:
        return json.dumps(
            {
                "applications_created": app_create.applications_created,
                "executes_started": execute.executes_started,
                "executes_completed": execute.executes_completed,
                "timing_by_action": timing.snapshot(),
            },
            indent=2,
        )

    return server


if __name__ == "__main__":
    build_server().run()
