"""Adaptive CRAG demo: hermetic tests via monkey-patching ``_call_granite``.

All Granite traffic is replaced with a queue of canned responses so
the FSM behavior is exercised without Ollama. Covers happy-path
short-circuit, one-round rewrite, max-rounds exhaustion, parse
failures, tolerant grade parsing, query rewrites threading through
retrieve, empty-corpus synthesis, citation provenance, input
validation, and the transition advertisement contract mid-loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

import adaptive_crag
import parallel_research
from adaptive_crag import (
    _format_snippets,
    _parse_grade,
    _retrieve_top,
    build_server,
)


def _patch_granite(monkeypatch, *responses: str):
    """Replace ``_call_granite`` with a queue of canned responses.

    Patching on the ``adaptive_crag`` module catches the re-exported
    binding the actions actually call. Running out of canned
    responses raises so a missing fixture is loud, not a hang.
    """
    queue = list(responses)

    async def fake_call(prompt, *, system=None, model=None):
        if not queue:
            raise AssertionError("ran out of canned Granite responses")
        return queue.pop(0)

    monkeypatch.setattr(adaptive_crag, "_call_granite", fake_call)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


# ── pure-function unit tests ────────────────────────────────────────


def test_retrieval_helpers_reuse_parallel_research_implementations():
    """The imported tokenizer + scorer + extractor are the same code
    powering the parallel-research demo, and they produce non-empty
    results against the shipped corpus."""
    services = parallel_research._load_corpus(parallel_research._DATA_DIR / "services")
    scored = parallel_research._score_documents("auth login", services)
    assert any(score > 0 for _, score in scored), "expected at least one match"
    top_name = scored[0][0]
    snippets = parallel_research._extract_snippets("auth login", services[top_name], 2)
    assert snippets, "expected at least one snippet for a real query"


def test_retrieve_top_returns_source_prefixed_keys():
    retrieved = _retrieve_top("how do I roll back a deploy", parallel_research._DATA_DIR)
    assert retrieved, "expected hits in the shipped corpus"
    for key in retrieved:
        assert "/" in key, f"expected '<source>/<doc>' key, got {key!r}"


@pytest.mark.asyncio
async def test_ask_accepts_custom_corpus_dir(monkeypatch, tmp_path):
    """Pointing at a user-supplied corpus directory makes retrieve
    search that directory instead of the shipped one."""
    # Build a tiny custom corpus: tmp_path/<source>/<doc>.md
    src = tmp_path / "ops"
    src.mkdir()
    (src / "rollback.md").write_text(
        "# Rollback runbook\n\nTo roll back, run `deploy-cli rollback`.\n"
    )
    # Granite gets canned responses for synth + grade.
    _patch_granite(monkeypatch, "Run deploy-cli rollback.", "5: grounded.")
    server = build_server()
    async with Client(server) as client:
        await client.call_tool(
            "step",
            {
                "action": "ask",
                "inputs": {"question": "how do I roll back?", "corpus_dir": str(tmp_path)},
            },
        )
        await client.call_tool("step", {"action": "retrieve", "inputs": {}})
        r = await client.call_tool("step", {"action": "retrieve", "inputs": {}})
        # State sanity: corpus_dir resolved to the custom path; retrieved
        # only includes docs from the custom corpus, not the shipped one.
        # (retrieve was already called once above; check current state.)
        await client.call_tool("step", {"action": "synthesize", "inputs": {}})
        r = await client.call_tool("step", {"action": "grade", "inputs": {}})
        out = r.structured_content
        assert out["state"]["corpus_dir"].rstrip("/") == str(tmp_path).rstrip("/")
        # Citations come from the custom corpus.
        assert any("ops/rollback.md" in k for k in out["state"]["retrieved"])
        assert not any(
            "services/" in k or "runbooks/" in k or "faqs/" in k for k in out["state"]["retrieved"]
        )


@pytest.mark.asyncio
async def test_ask_rejects_nonexistent_corpus_dir():
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {
                "action": "ask",
                "inputs": {
                    "question": "x",
                    "corpus_dir": "/tmp/definitely-does-not-exist-xyz123",
                },
            },
        )
        out = r.structured_content
        assert out["error"] == "action_error"
        assert "does not exist" in out["error_message"]


def test_parse_grade_tolerates_whitespace_and_missing_reason():
    """The parser strips surrounding whitespace, accepts a bare
    integer, and reports a parse failure on anything that isn't an
    integer in 1-5."""
    assert _parse_grade("4: solid answer") == (4, "solid answer")
    score, reason = _parse_grade("  5 : excellent  ")
    assert score == 5
    assert reason == "excellent"
    assert _parse_grade("5") == (5, "")
    score, reason = _parse_grade("I think it's pretty good actually")
    assert score == 0
    assert reason.startswith("parse_failed:")


def test_format_snippets_handles_empty_dict():
    assert "no snippets" in _format_snippets({}).lower()


# ── happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_grade_five_first_try_finalizes(monkeypatch):
    """Grade 5 on the first round routes straight to finalize with
    one round taken and the original query as the only one tried."""
    _patch_granite(
        monkeypatch,
        "Roll back with deploy-cli rollback <service> <release-id>.",  # synthesize
        "5: Grounded and on-point.",  # grade
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["state"]["grade_score"] == 5
        assert "finalize" in out["valid_next_actions"]
        assert "rewrite_query" not in out["valid_next_actions"]
        out = _payload(await _step(client, "finalize"))
        final = out["state"]["final_answer"]
        assert final["final_grade"] == 5
        assert final["rounds_taken"] == 1
        assert final["search_queries_tried"] == ["how do I roll back a deploy?"]


# ── one round of rewrite then finalize ──────────────────────────────


@pytest.mark.asyncio
async def test_one_round_rewrite_then_finalize(monkeypatch):
    """Grade 2 triggers a rewrite, second pass grades 4, FSM
    finalizes. Two rounds taken, two queries tried."""
    _patch_granite(
        monkeypatch,
        "Some draft answer.",  # synthesize #1
        "2: weak grounding, missed the rollback command.",  # grade #1
        "deploy-cli rollback command usage",  # rewrite_query
        "Run deploy-cli rollback <service> <release-id>.",  # synthesize #2
        "4: Now grounded in the runbook.",  # grade #2
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["state"]["grade_score"] == 2
        assert "rewrite_query" in out["valid_next_actions"]
        assert "finalize" not in out["valid_next_actions"]
        await _step(client, "rewrite_query")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["state"]["grade_score"] == 4
        out = _payload(await _step(client, "finalize"))
        final = out["state"]["final_answer"]
        assert final["rounds_taken"] == 2
        assert len(final["search_queries_tried"]) == 2


# ── max rounds exhausted ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_rounds_exhausted_finalizes_with_last_attempt(monkeypatch):
    """Three bad grades in a row -> finalize with the last draft and
    last grade. The rewrite branch closes once we hit the cap."""
    _patch_granite(
        monkeypatch,
        "Draft 1.",  # synthesize #1
        "2: bad",  # grade #1
        "rewritten 1",  # rewrite #1
        "Draft 2.",  # synthesize #2
        "1: still bad",  # grade #2
        "rewritten 2",  # rewrite #2
        "Draft 3.",  # synthesize #3
        "3: meh",  # grade #3 -> caps; only finalize valid
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        for _ in range(2):
            await _step(client, "retrieve")
            await _step(client, "synthesize")
            await _step(client, "grade")
            await _step(client, "rewrite_query")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["valid_next_actions"] == ["finalize"]
        out = _payload(await _step(client, "finalize"))
        final = out["state"]["final_answer"]
        assert final["rounds_taken"] == 3
        assert final["final_grade"] == 3
        assert final["answer"] == "Draft 3."


# ── parse failures count as bad grades ──────────────────────────────


@pytest.mark.asyncio
async def test_grade_parse_failures_count_as_rounds(monkeypatch):
    """Three unparseable grader responses count as three bad rounds
    and finalize with grade_score=0."""
    _patch_granite(
        monkeypatch,
        "Draft 1.",
        "I think it's pretty good actually",  # parse fail
        "next query 1",
        "Draft 2.",
        "no idea honestly",  # parse fail
        "next query 2",
        "Draft 3.",
        "well, maybe?",  # parse fail
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        for _ in range(2):
            await _step(client, "retrieve")
            await _step(client, "synthesize")
            await _step(client, "grade")
            await _step(client, "rewrite_query")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["state"]["grade_score"] == 0
        assert out["valid_next_actions"] == ["finalize"]
        out = _payload(await _step(client, "finalize"))
        final = out["state"]["final_answer"]
        assert final["rounds_taken"] == 3
        assert final["final_grade"] == 0
        assert final["final_grade_reason"].startswith("parse_failed:")


# ── rewrite_query threads the new query into retrieve ───────────────


@pytest.mark.asyncio
async def test_rewrite_query_appends_and_retrieve_uses_latest(monkeypatch):
    """The rewritten query lands as ``search_queries[-1]`` and the
    next ``retrieve`` keys its scoring off that, not the original."""
    _patch_granite(
        monkeypatch,
        "Draft 1.",  # synthesize #1
        "2: bad",  # grade #1
        "deploy-cli rollback fleet propagation",  # rewrite
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="random unrelated question xyzzy")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        await _step(client, "grade")
        out = _payload(await _step(client, "rewrite_query"))
        queries = out["state"]["search_queries"]
        assert queries[-1] == "deploy-cli rollback fleet propagation"
        assert len(queries) == 2
        out = _payload(await _step(client, "retrieve"))
        # The rewritten query has real corpus matches; the original didn't.
        assert out["state"]["retrieved"], "expected retrieval to hit on the new query"


# ── empty corpus retrieval still flows ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_retrieval_still_synthesizes_and_grades(monkeypatch):
    """A query that doesn't hit any corpus terms yields an empty
    ``retrieved`` dict, but synthesize + grade still run and the
    pipeline can decide to rewrite."""
    _patch_granite(
        monkeypatch,
        "I don't have enough context to answer.",  # synthesize
        "1: no grounding because no snippets",  # grade
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="zzzqqq nonsense xyzzy")
        out = _payload(await _step(client, "retrieve"))
        assert out["state"]["retrieved"] == {}
        await _step(client, "synthesize")
        out = _payload(await _step(client, "grade"))
        assert out["state"]["grade_score"] == 1
        assert "rewrite_query" in out["valid_next_actions"]


# ── citations come from retrieved keys ──────────────────────────────


@pytest.mark.asyncio
async def test_citations_match_retrieved_doc_names(monkeypatch):
    """``final_answer.citations`` is exactly the doc-names dict the
    final draft was synthesized against."""
    _patch_granite(
        monkeypatch,
        "Roll back with deploy-cli rollback <service> <release-id>.",
        "5: Grounded.",
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        out = _payload(await _step(client, "retrieve"))
        retrieved_keys = list(out["state"]["retrieved"].keys())
        await _step(client, "synthesize")
        await _step(client, "grade")
        out = _payload(await _step(client, "finalize"))
        assert out["state"]["final_answer"]["citations"] == retrieved_keys


# ── input validation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_with_empty_question_is_refused(monkeypatch):
    _patch_granite(monkeypatch)
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "ask", question="   "))
        assert out["error"] == "action_error"
        assert "question must not be empty" in out["error_message"]


# ── transition advertisement contract ───────────────────────────────


@pytest.mark.asyncio
async def test_burr_next_advertises_rewrite_not_finalize_mid_loop(monkeypatch):
    """After a grade of 3 at round 1 of 3, ``burr://next`` should
    advertise rewrite_query and NOT finalize."""
    _patch_granite(
        monkeypatch,
        "Draft 1.",
        "3: borderline",
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        await _step(client, "grade")
        nxt = json.loads((await client.read_resource("burr://next"))[0].text)
        assert "rewrite_query" in nxt
        assert "finalize" not in nxt


@pytest.mark.asyncio
async def test_burr_next_advertises_finalize_not_rewrite_at_cap(monkeypatch):
    """After a grade of 3 at round 3 of 3, ``burr://next`` should
    advertise finalize and NOT rewrite_query."""
    _patch_granite(
        monkeypatch,
        "Draft 1.",
        "3: borderline",
        "next query 1",
        "Draft 2.",
        "3: borderline",
        "next query 2",
        "Draft 3.",
        "3: still borderline",
    )
    server = build_server()
    async with Client(server) as client:
        await _step(client, "ask", question="how do I roll back a deploy?")
        for _ in range(2):
            await _step(client, "retrieve")
            await _step(client, "synthesize")
            await _step(client, "grade")
            await _step(client, "rewrite_query")
        await _step(client, "retrieve")
        await _step(client, "synthesize")
        await _step(client, "grade")
        nxt = json.loads((await client.read_resource("burr://next"))[0].text)
        assert "finalize" in nxt
        assert "rewrite_query" not in nxt
