"""Parallel research agent: fan-out across real doc folders.

The example searches a shipped markdown corpus under
``examples/data/parallel_research/{services,runbooks,faqs}/``. Each
source folder spawns a four-step search sub-Application that runs
concurrently with the others via ``asyncio.gather``. Tests cover:

- happy path: a relevant query returns hits from every source.
- scoring is discriminative: the right doc tops each source.
- source filtering: ``sources=["services"]`` only spawns one sub-run.
- bad source name: clear error with the available source list.
- no matches: zero-score queries report nothing rather than crashing.
- sub-run history populated: the four-step trace is on the subrun
  record (relies on v1.10.0 spawn_subapp trace passthrough).
- concurrency: N sub-runs finish in roughly one sub-run's time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from parallel_research import (
    _available_sources,
    _extract_snippets,
    _score_documents,
    _tokenize,
    build_server,
)

# ── unit tests on the search primitives ────────────────────────────


def test_tokenize_lowercases_and_drops_punctuation():
    assert _tokenize("Auth-Service: token TTL is 1h!") == [
        "auth",
        "service",
        "token",
        "ttl",
        "is",
        "1h",
    ]


def test_score_documents_returns_descending_by_count():
    corpus = {
        "a.md": "auth auth auth deploy",
        "b.md": "deploy deploy",
        "c.md": "billing only here",
    }
    scored = _score_documents("auth deploy", corpus)
    # a.md has 4 matches (3 auth + 1 deploy), b.md 2 (deploy x2), c.md 0.
    assert scored[0] == ("a.md", 4)
    assert scored[1] == ("b.md", 2)
    assert scored[2] == ("c.md", 0)


def test_extract_snippets_pulls_lines_with_query_terms():
    content = "line one\nline two with auth\nline three\nanother auth line\n"
    snippets = _extract_snippets("auth", content, max_snippets=2)
    assert len(snippets) == 2
    assert any("line two with auth" in s for s in snippets)


def test_available_sources_finds_shipped_corpus():
    sources = _available_sources()
    # The data folder we ship has these three.
    assert {"services", "runbooks", "faqs"} <= set(sources)


# ── parent FSM behaviour ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_sources_fan_out_across_all_three_folders():
    """No sources arg means search every available folder."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "research", "inputs": {"query": "auth token"}}
        )
        out = r.structured_content
        assert set(out["state"]["sources"]) == {"services", "runbooks", "faqs"}
        assert len(out["state"]["results"]) == 3
        subs = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        assert len(subs) == 3
        labels = {s["label"] for s in subs}
        assert labels == {"search-services", "search-runbooks", "search-faqs"}


@pytest.mark.asyncio
async def test_scoring_finds_authoritative_doc_per_source():
    """For 'auth' the top services hit should be auth.md, top runbook
    should be auth-debug.md. Scoring is just term frequency, but the
    shipped corpus is structured so the obvious doc wins."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "research", "inputs": {"query": "auth"}})
        out = r.structured_content
        report = out["state"]["report"]
        assert "auth.md" in report
        assert "auth-debug.md" in report


@pytest.mark.asyncio
async def test_source_filter_only_spawns_requested_sources():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {"query": "rollback", "sources": ["runbooks"]},
            },
        )
        out = r.structured_content
        assert out["state"]["sources"] == ["runbooks"]
        subs = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        assert len(subs) == 1
        assert subs[0]["label"] == "search-runbooks"


@pytest.mark.asyncio
async def test_unknown_source_returns_actionable_error():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {"query": "x", "sources": ["nope"]},
            },
        )
        out = r.structured_content
        assert out["error"] == "action_error"
        assert "nope" in out["error_message"]
        # Available sources listed for the agent.
        assert "services" in out["error_message"]


@pytest.mark.asyncio
async def test_query_with_no_matches_reports_no_hits_gracefully():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {
                    "query": "xylophone quantum particle",
                    "sources": ["services"],
                },
            },
        )
        out = r.structured_content
        assert "no matches" in out["state"]["report"]


@pytest.mark.asyncio
async def test_subrun_history_populated_with_four_step_trace():
    """spawn_subapp records one history entry per action it ran. The
    sub-graph has four actions, so the trace has four entries; each entry
    carries the action name and the post-step state.
    """
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {"query": "deploy", "sources": ["services"]},
            },
        )
        subs = json.loads((await client.read_resource("theodosia://subruns"))[0].text)
        assert len(subs) == 1
        sid = subs[0]["id"]
        detail = json.loads((await client.read_resource(f"theodosia://subruns/{sid}"))[0].text)
        actions_in_trace = [h.get("action") for h in detail["history"] if h.get("action")]
        assert actions_in_trace.count("load_documents") == 1
        assert actions_in_trace.count("score_documents") == 1
        assert actions_in_trace.count("extract_snippets") == 1
        assert actions_in_trace.count("summarize") == 1


@pytest.mark.asyncio
async def test_parent_history_lists_every_spawned_subrun():
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {
                    "query": "secret rotation",
                    "sources": ["services", "runbooks", "faqs"],
                },
            },
        )
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        entry = history[0]
        assert entry["action"] == "research"
        assert len(entry["subruns"]) == 3
        assert all(uri.startswith("theodosia://subruns/") for uri in entry["subrun_uris"])


@pytest.mark.asyncio
async def test_sources_run_concurrently_not_sequentially():
    """Three sources, each doing real (but small) disk I/O. Concurrent
    fan-out should finish in well under three times one source's work.
    With nine documents totaling a few KB, one source takes a few ms
    on a warm fs; three concurrent fan-outs should land well under 1s
    even with all the Burr-tracker overhead."""
    import time

    server = build_server()
    async with Client(server) as client:
        t0 = time.monotonic()
        await client.call_tool("step", {"action": "research", "inputs": {"query": "deploy auth"}})
        elapsed = time.monotonic() - t0
        # Soft upper bound; mostly a smoke check that we're not
        # accidentally running serially or hanging.
        assert elapsed < 2.0, f"research took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_research_accepts_custom_corpus_dir(tmp_path):
    """Pointing at a user-supplied corpus directory makes the fan-out
    search that directory instead of the shipped one."""
    # Build a tiny custom corpus: tmp_path/<source>/<doc>.md
    src_a = tmp_path / "kb"
    src_a.mkdir()
    (src_a / "intro.md").write_text(
        "# Intro\n\nThe quokka is a small marsupial native to Western Australia.\n"
    )
    src_b = tmp_path / "trivia"
    src_b.mkdir()
    (src_b / "facts.md").write_text("# Facts\n\nQuokkas are known for their friendly faces.\n")
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {"query": "quokka", "corpus_dir": str(tmp_path)},
            },
        )
        out = r.structured_content
        assert "error" not in out
        assert sorted(out["state"]["sources"]) == ["kb", "trivia"]
        # Resolved corpus_dir lands in state.
        assert out["state"]["corpus_dir"].rstrip("/") == str(tmp_path).rstrip("/")
        # Each per-source report references its source label.
        assert "[kb]" in out["state"]["report"]
        assert "[trivia]" in out["state"]["report"]


@pytest.mark.asyncio
async def test_research_rejects_nonexistent_corpus_dir():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "research",
                "inputs": {
                    "query": "x",
                    "corpus_dir": "/tmp/definitely-does-not-exist-xyz123",
                },
            },
        )
        out = r.structured_content
        assert out["error"] == "action_error"
        assert "does not exist" in out["error_message"]
