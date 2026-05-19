"""Parallel sub-application spawn via asyncio.gather.

Burr's parallelism module is one shape; another shape that needs
nothing new from burr-mcp is using ``spawn_subapp`` inside a parent
action and ``asyncio.gather`` to fan out. Each sub-Application is
its own ``burr://subruns/{id}`` entry, runs concurrently with its
siblings, and the parent action gathers the results into the parent
state.

This file demonstrates a research workflow that fans out queries
across three sources in parallel and synthesises the findings. The
sources are stubs (no external services), so the example is
self-contained and the parallelism is real.

Run as a stdio server:

    uv run python examples/parallel_research.py

The parent FSM has one action, ``research``, that takes a query
string and a list of sources. Inside ``research``, three sub-runs
fire concurrently via ``asyncio.gather``; their findings come back
as a list. The aggregate report writes to parent state.
"""

from __future__ import annotations

import asyncio

from burr.core import ApplicationBuilder, State, action

from burr_mcp import ServingMode, mount, spawn_subapp

# ── sub-graph: search one source ────────────────────────────────────


@action(reads=["query", "source"], writes=["found"])
async def search_one(state: State) -> State:
    """Stub: pretend to query a source. Sleeps briefly so parallelism
    is observable in the trace."""
    query = state.get("query", "")
    source = state.get("source", "")
    await asyncio.sleep(0.05)
    return state.update(found=f"[{source}] result for '{query}'")


def _build_search_subgraph(query: str, source: str):
    return (
        ApplicationBuilder()
        .with_actions(search_one=search_one)
        .with_state(query=query, source=source, found=None)
        .with_entrypoint("search_one")
        .build()
    )


# ── parent action that fans out ─────────────────────────────────────


@action(reads=[], writes=["query", "sources", "results", "report"])
async def research(state: State, query: str, sources: list[str]) -> State:
    """Run search_one concurrently across the named sources and gather
    the findings into a single report.

    Each sub-run is recorded under ``burr://subruns/{id}`` with the
    source as its label, so the agent can inspect any individual
    source's trace without affecting the parent's view. The session's
    parent history entry for ``research`` will list all the spawned
    sub-run ids in its ``subruns`` field.
    """
    results = await asyncio.gather(
        *(
            spawn_subapp(
                _build_search_subgraph(query, source),
                label=f"search-{source}",
            )
            for source in sources
        )
    )
    findings = [r["final_state"]["found"] for r in results]
    report = "; ".join(findings)
    return state.update(
        query=query,
        sources=list(sources),
        results=findings,
        report=report,
    )


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(research=research)
        .with_state(query=None, sources=None, results=None, report=None)
        .with_entrypoint("research")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="parallel-research",
        instructions=(
            "Parallel research fan-out. Call research(query, sources) "
            "where sources is a list of strings; the action spawns one "
            "sub-app per source concurrently via asyncio.gather. Each "
            "sub-run appears at burr://subruns/{id}. The parent "
            "state's report joins the findings."
        ),
    )


if __name__ == "__main__":
    build_server().run()
