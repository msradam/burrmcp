"""Subgraph mounting example.

Parent FSM has one action ``investigate`` that spawns a sub-Application
with three steps (``gather``, ``analyse``, ``report``). The sub-run's
timeline is recorded under ``burr://subruns/{id}`` and the parent
history entry for ``investigate`` carries the new subrun id under
``subruns: [<id>]`` so a client can correlate parent action with
child timeline.

Run:

    python examples/subgraphs.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action

from burr_mcp import ServingMode, mount, spawn_subapp

# ── sub-graph ────────────────────────────────────────────────────────


@action(reads=[], writes=["gathered"])
async def gather(state: State, source: str) -> State:
    return state.update(gathered=f"data from {source}")


@action(reads=["gathered"], writes=["analysis"])
async def analyse(state: State) -> State:
    g = state.get("gathered", "")
    return state.update(analysis=f"analysed: {g}")


@action(reads=["analysis"], writes=["report"])
async def report(state: State) -> State:
    return state.update(report=f"report based on {state.get('analysis')}")


def build_subgraph(source: str):
    """Build a fresh sub-Application for one investigation."""
    return (
        ApplicationBuilder()
        .with_actions(gather=gather, analyse=analyse, report=report)
        .with_transitions(("gather", "analyse"), ("analyse", "report"))
        .with_state(gathered=None, analysis=None, report=None)
        .with_entrypoint("gather")
        .build()
    )


# ── parent graph ─────────────────────────────────────────────────────


@action(reads=[], writes=["investigation_report"])
async def investigate(state: State, source: str) -> State:
    """Run the three-step investigation sub-graph against ``source``.

    Uses ``burr_mcp.spawn_subapp`` to delegate. The sub-run's per-step
    timeline appears at ``burr://subruns/{id}`` automatically.
    """
    sub = build_subgraph(source)
    result = await spawn_subapp(sub, label=f"investigate({source})", inputs={"source": source})
    return state.update(investigation_report=result["final_state"].get("report"))


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(investigate=investigate)
        .with_state(investigation_report=None)
        .with_entrypoint("investigate")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="subgraphs",
        instructions=(
            "Investigation FSM that delegates to a sub-graph. Read "
            "burr://subruns to list sub-runs spawned in this session "
            "and burr://subruns/{id} for the full sub-run timeline."
        ),
    )


if __name__ == "__main__":
    build_server().run()
