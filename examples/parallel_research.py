"""Parallel research agent over a local doc corpus.

A parent FSM with one action, ``research(query, sources)``, that fans
out across multiple source folders concurrently. Each source is a
subfolder of ``examples/data/parallel_research/`` containing markdown
documents. For each source, a four-step sub-Application runs:

    load_documents -> score_documents -> extract_snippets -> summarize

All sub-Applications run concurrently via ``asyncio.gather``, each
appearing as its own ``burr://subruns/{id}`` entry with the source
folder as its label. The parent collects the per-source mini-reports
into a single combined report.

Shipped corpus:

    examples/data/parallel_research/
      services/   auth.md, billing.md, deploy.md
      runbooks/   incident-response.md, auth-debug.md, deploy-rollback.md
      faqs/       oncall.md, secrets.md, deployments.md

The search itself is plain term-frequency scoring plus line-context
snippet extraction. No external dependencies, no web calls. Replace
``_score_documents`` and ``_extract_snippets`` with anything you want
(BM25, embeddings, a hosted retrieval API) without touching the FSM
shape.

Run as a stdio server:

    uv run python examples/parallel_research.py

Try queries like:

    research(query="auth rotation policy")
    research(query="how do I roll back a deploy", sources=["runbooks"])
    research(query="paged at 3am what now", sources=["runbooks", "faqs"])
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount, spawn_subapp

_TRACKER_PROJECT = "parallel-research-demo"
_DATA_DIR = Path(__file__).parent / "data" / "parallel_research"

_TOP_DOCS_PER_SOURCE = 3
_SNIPPETS_PER_DOC = 2


# ── search primitives (pure functions, swap out for anything) ───────


def _tokenize(text: str) -> list[str]:
    """Split text into lowercased alphanumeric tokens."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _load_corpus(folder: Path) -> dict[str, str]:
    """Return ``{filename: content}`` for every ``*.md`` under ``folder``."""
    return {p.name: p.read_text() for p in sorted(folder.glob("*.md"))}


def _score_documents(query: str, corpus: dict[str, str]) -> list[tuple[str, int]]:
    """Score every document by query-term frequency.

    Returns ``[(filename, score)]`` sorted by score descending. Zero-
    score docs are kept so the agent can see what was searched even
    when nothing matched.
    """
    terms = set(_tokenize(query))
    scored: list[tuple[str, int]] = []
    for name, content in corpus.items():
        counts = Counter(_tokenize(content))
        score = sum(counts.get(t, 0) for t in terms)
        scored.append((name, score))
    scored.sort(key=lambda kv: -kv[1])
    return scored


def _extract_snippets(query: str, content: str, max_snippets: int) -> list[str]:
    """Pull lines containing any query term, plus one line of context."""
    terms = set(_tokenize(query))
    if not terms:
        return []
    lines = content.splitlines()
    matches: list[str] = []
    for i, line in enumerate(lines):
        if any(t in line.lower() for t in terms):
            ctx_start = max(0, i - 1)
            ctx_end = min(len(lines), i + 2)
            snippet = "\n".join(lines[ctx_start:ctx_end]).strip()
            if snippet and snippet not in matches:
                matches.append(snippet)
            if len(matches) >= max_snippets:
                break
    return matches


def _available_sources() -> list[str]:
    """List subfolders of the shipped corpus (one per source)."""
    if not _DATA_DIR.exists():
        return []
    return sorted(p.name for p in _DATA_DIR.iterdir() if p.is_dir())


# ── sub-graph: one source's four-step search ────────────────────────


@action(reads=["source"], writes=["documents"])
async def load_documents(state: State) -> State:
    """Read every ``*.md`` document under ``data/parallel_research/<source>``.

    Refuses with a clear error if the source folder doesn't exist; the
    parent ``research`` action validates source names up front, so this
    only fires if the corpus has been moved or deleted on disk.
    """
    source = state["source"]
    folder = _DATA_DIR / source
    if not folder.exists():
        raise FileNotFoundError(
            f"source folder not found on disk: {folder}. Available: {_available_sources()}"
        )
    docs = _load_corpus(folder)
    return state.update(documents=docs)


@action(reads=["query", "documents"], writes=["scored"])
async def score_documents(state: State) -> State:
    """Score every loaded document by query-term frequency."""
    scored = _score_documents(state["query"], state["documents"])
    return state.update(scored=scored)


@action(reads=["query", "documents", "scored"], writes=["findings"])
async def extract_snippets(state: State) -> State:
    """Pull short snippets from the top-scoring documents.

    Skips zero-score documents (no query terms matched) so the
    findings list only contains docs the search actually hit.
    """
    findings: list[dict] = []
    for doc_name, score in state["scored"][:_TOP_DOCS_PER_SOURCE]:
        if score == 0:
            continue
        snippets = _extract_snippets(
            state["query"], state["documents"][doc_name], _SNIPPETS_PER_DOC
        )
        findings.append({"doc": doc_name, "score": score, "snippets": snippets})
    return state.update(findings=findings)


@action(reads=["query", "source", "findings"], writes=["report"])
async def summarize(state: State) -> State:
    """Render the per-source mini-report from the findings."""
    query = state["query"]
    source = state["source"]
    findings = state["findings"]
    if not findings:
        return state.update(report=f"[{source}] no matches for {query!r}.")
    lines = [f"[{source}] {len(findings)} hit(s) for {query!r}:"]
    for f in findings:
        lines.append(f"  - {f['doc']} (score={f['score']})")
        for snippet in f["snippets"]:
            first_line = snippet.splitlines()[0].strip()
            lines.append(f"      > {first_line}")
    return state.update(report="\n".join(lines))


def _build_search_subgraph(query: str, source: str):
    """Build a fresh four-step search sub-Application for one source.

    State seeds ``query`` and ``source`` at construction time so the
    spawned sub-app doesn't need to thread them through ``inputs``.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            load_documents=load_documents,
            score_documents=score_documents,
            extract_snippets=extract_snippets,
            summarize=summarize,
        )
        .with_transitions(
            ("load_documents", "score_documents"),
            ("score_documents", "extract_snippets"),
            ("extract_snippets", "summarize"),
        )
        .with_tracker(LocalTrackingClient(project=f"{_TRACKER_PROJECT}-search"))
        .with_state(
            query=query,
            source=source,
            documents={},
            scored=[],
            findings=[],
            report=None,
        )
        .with_entrypoint("load_documents")
        .build()
    )


# ── parent: fan-out across sources ──────────────────────────────────


@action(reads=[], writes=["query", "sources", "results", "report"])
async def research(state: State, query: str, sources: list[str] | None = None) -> State:
    """Fan out a research query across one or more source folders.

    Args:
        query: The research question. Tokenised and matched against
            document contents in each source.
        sources: Optional list of source folder names. Defaults to all
            available sources (every subfolder under
            ``examples/data/parallel_research/``). Pass a subset to
            scope the search.

    Each source spawns one search sub-Application that runs the
    four-step pipeline (load_documents, score_documents,
    extract_snippets, summarize) concurrently with the others via
    ``asyncio.gather``. Each sub-run is recorded at
    ``burr://subruns/{id}`` with the source name as its label, so an
    MCP client can drill into one source's timeline without the
    others getting in the way.

    Returns ``state.report`` joined from the per-source reports plus
    ``state.results`` (a list of per-source report strings).
    """
    available = _available_sources()
    if not available:
        raise RuntimeError(
            f"no source folders under {_DATA_DIR}. Did the data corpus ship with this example?"
        )
    target_sources = list(sources) if sources else available
    if not target_sources:
        raise ValueError(f"must search at least one source. Available: {available}")
    bad = [s for s in target_sources if s not in available]
    if bad:
        raise ValueError(f"unknown source(s) {bad}. Available: {available}")
    results = await asyncio.gather(
        *(
            spawn_subapp(
                _build_search_subgraph(query, source),
                label=f"search-{source}",
            )
            for source in target_sources
        )
    )
    per_source_reports = [r["final_state"]["report"] for r in results]
    return state.update(
        query=query,
        sources=target_sources,
        results=per_source_reports,
        report="\n\n".join(per_source_reports),
    )


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(research=research)
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(query=None, sources=None, results=None, report=None)
        .with_entrypoint("research")
        .build()
    )


def build_server():
    available = _available_sources()
    sources_hint = ", ".join(available) if available else "(none on disk)"
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="parallel-research",
        instructions=(
            "Parallel research agent over a local markdown corpus at "
            "examples/data/parallel_research/. Call "
            "research(query, sources) where query is a freeform string "
            "and sources is an optional list of source-folder names "
            f"(available: {sources_hint}). Defaults to all sources. "
            "Each source spawns a concurrent four-step search "
            "sub-Application (load_documents, score_documents, "
            "extract_snippets, summarize). The combined report joins "
            "every per-source mini-report. Each sub-run is addressable "
            "at burr://subruns/{id} with the source name as its label."
        ),
    )


if __name__ == "__main__":
    build_server().run()
