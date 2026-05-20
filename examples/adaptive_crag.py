"""Self-correcting RAG over a local markdown corpus, with the FSM
grading its own output and rewriting the search query when grounding
is weak.

The shape is a simplified port of CRAG (Yan et al 2024,
arxiv:2401.15884) collapsed into a six-action Burr FSM:

    ask -> retrieve -> synthesize -> grade
                                  -> finalize       (good enough)
                                  -> rewrite_query  (try again)
    rewrite_query -> retrieve

Every loop iteration is a separate visible step in ``burr://history``
and ``burr://trace``. The retry-as-transition pattern is the same one
used in ``granite_oncall``: instead of a Python ``while`` loop inside
one action, each round of grade-then-route is encoded as transitions
so an operator can step through one re-retrieval at a time.

Granite (via Ollama) is called three times per round at most:

* ``synthesize`` -- write an answer from the retrieved snippets.
* ``grade`` -- score the draft 1-5 on grounding plus relevance.
* ``rewrite_query`` -- propose a new search query when the grade is
  below 4.

The retrieval primitives are imported from ``parallel_research``
unchanged so the two demos share one corpus and one search
implementation. The corpus lives at
``examples/data/parallel_research/{services,runbooks,faqs}/``.

Loops are capped at 3 rounds. After the cap, ``finalize`` runs with
whatever the last draft answer and grade were, plus the full audit
trail of attempts and the queries that were tried.

Requires Ollama running with a Granite model pulled:

    ollama serve &
    ollama pull granite4.1:3b
    python examples/adaptive_crag.py

Override the model or endpoint via the same env vars as granite_oncall:

    BURR_MCP_GRANITE_MODEL=granite4:1b
    BURR_MCP_GRANITE_OLLAMA_BASE=http://other-host:11434
"""

from __future__ import annotations

import re
from pathlib import Path

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

# Reuse the granite call + error type from the oncall demo so tests can
# monkey-patch one symbol and both examples stay aligned on env vars.
from granite_oncall import GraniteUnavailable, _call_granite  # noqa: F401  (re-exported for tests)

# Reuse the retrieval primitives from the parallel-research demo.
from parallel_research import (
    _available_sources,
    _extract_snippets,
    _load_corpus,
    _score_documents,
)

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "adaptive-crag-demo"
_DATA_DIR = Path(__file__).parent / "data" / "parallel_research"

_TOP_DOCS = 3
_SNIPPETS_PER_DOC = 2
_DEFAULT_MAX_ROUNDS = 3
_PASS_GRADE = 4


# ── prompts ─────────────────────────────────────────────────────────


_SYNTH_SYSTEM = (
    "Answer the question using ONLY the retrieved snippets. If they "
    "don't have enough information, reply with 'I don't have enough "
    "context to answer.' Keep the answer to 2-3 sentences."
)

_SYNTH_PROMPT = """Question: {question}

Retrieved snippets:
{formatted_snippets}

Answer the question using ONLY the snippets above. 2-3 sentences."""

_GRADER_SYSTEM = (
    "You are a strict grader. Reply with exactly one integer 1-5, a colon, and a one-line reason."
)

_GRADER_PROMPT = """Question: {question}
Answer: {draft_answer}

Retrieved snippets the answer should be based on:
{formatted_snippets}

Score 1-5 on whether the answer is GROUNDED in the snippets (not just plausible) \
AND RELEVANT to the question.
5 = both grounded and relevant.
1 = neither grounded nor relevant.

Reply with ONLY the format: "<integer>: <one-line reason>"
Example: "4: Mostly grounded, missed the rate-limit detail."
"""

_REWRITER_SYSTEM = (
    "You are a search query rewriter. Reply with ONLY the new query string, nothing else."
)

_REWRITER_PROMPT = """The previous answer was graded {grade_score}/5. Grader said: "{grade_reason}"
Question: {question}
Previous search query: "{previous_query}"

Propose a better search query for finding more relevant snippets. Reply with ONLY the new query."""


# ── helpers ─────────────────────────────────────────────────────────


_GRADE_RE = re.compile(r"^\s*([1-5])\s*(?::\s*(.*))?\s*$")


def _format_snippets(retrieved: dict[str, list[str]]) -> str:
    """Render the retrieved snippets as a markdown-ish block keyed by
    source-prefixed doc name."""
    if not retrieved:
        return "(no snippets retrieved)"
    blocks: list[str] = []
    for doc, snippets in retrieved.items():
        body = "\n\n".join(snippets) if snippets else "(no snippet text)"
        blocks.append(f"### {doc}\n{body}")
    return "\n\n---\n\n".join(blocks)


def _parse_grade(raw: str) -> tuple[int, str]:
    """Parse "<int>: <reason>" tolerantly.

    Accepts surrounding whitespace, optional reason (a bare digit is
    fine and yields an empty reason), and rejects anything else with
    ``(0, "parse_failed: ...")`` so the FSM can treat parse failure as
    a bad grade rather than crashing.
    """
    if not raw:
        return 0, "parse_failed: <empty>"
    m = _GRADE_RE.match(raw)
    if not m:
        return 0, f"parse_failed: {raw[:60]}"
    score = int(m.group(1))
    reason = (m.group(2) or "").strip()
    return score, reason


def _resolve_corpus_dir(corpus_dir: str | None) -> Path:
    """Resolve a user-supplied corpus directory to an absolute path.

    Empty / None falls back to the shipped corpus. Raises ValueError
    if the resolved path doesn't exist or isn't a directory.
    """
    if not corpus_dir:
        return _DATA_DIR
    p = Path(corpus_dir).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"corpus_dir does not exist: {corpus_dir}")
    if not p.is_dir():
        raise ValueError(f"corpus_dir is not a directory: {corpus_dir}")
    return p


def _retrieve_top(query: str, corpus_dir: Path) -> dict[str, list[str]]:
    """Score every doc across every source folder under ``corpus_dir``
    and return the top-N overall with snippets per doc.

    Keys are ``"<source>/<doc_name>"`` so the caller (and citations)
    can tell which corpus each snippet came from.
    """
    scored_all: list[tuple[str, str, int, str]] = []  # (source, name, score, content)
    for source in _available_sources(corpus_dir):
        folder = corpus_dir / source
        corpus = _load_corpus(folder)
        for name, score in _score_documents(query, corpus):
            if score <= 0:
                continue
            scored_all.append((source, name, score, corpus[name]))
    scored_all.sort(key=lambda t: -t[2])
    retrieved: dict[str, list[str]] = {}
    for source, name, _score, content in scored_all[:_TOP_DOCS]:
        snippets = _extract_snippets(query, content, _SNIPPETS_PER_DOC)
        retrieved[f"{source}/{name}"] = snippets
    return retrieved


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "question",
        "search_queries",
        "corpus_dir",
        "retrieved",
        "draft_answer",
        "grade_score",
        "grade_reason",
        "grading_attempts",
        "max_rounds",
        "final_answer",
        "log",
    ],
)
async def ask(
    state: State,
    question: str,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    corpus_dir: str | None = None,
) -> State:
    """Entrypoint. Validates the question and resets all state.

    Args:
        question: The research question.
        max_rounds: Hard cap on grade-then-rewrite cycles. Default 3.
        corpus_dir: Optional path to a directory of markdown documents
            organised into per-source subfolders. Defaults to the
            shipped corpus at ``examples/data/parallel_research/``.
            Supports ``~`` and relative paths.

    ``search_queries`` is seeded with the original question; the
    rewrite loop appends to it.
    """
    if not question.strip():
        raise ValueError("question must not be empty")
    base = _resolve_corpus_dir(corpus_dir)
    return state.update(
        question=question,
        search_queries=[question],
        corpus_dir=str(base),
        retrieved={},
        draft_answer=None,
        grade_score=0,
        grade_reason="",
        grading_attempts=[],
        max_rounds=max_rounds,
        final_answer=None,
        log=[f"Question received: {question[:80]!r} (max_rounds={max_rounds}, corpus_dir={base})"],
    )


@action(reads=["search_queries", "corpus_dir", "log"], writes=["retrieved", "log"])
async def retrieve(state: State) -> State:
    """Score every doc under every corpus source by the latest query
    and pull snippets from the top hits.

    Always reads ``search_queries[-1]`` so the loop after
    ``rewrite_query`` automatically picks up the new query.
    """
    query = state["search_queries"][-1]
    base = Path(state["corpus_dir"]) if state.get("corpus_dir") else _DATA_DIR
    retrieved = _retrieve_top(query, base)
    return state.update(
        retrieved=retrieved,
        log=[
            *state.get("log", []),
            f"Retrieved {len(retrieved)} doc(s) for query={query!r} from {base}",
        ],
    )


@action(reads=["question", "retrieved", "log"], writes=["draft_answer", "log"])
async def synthesize(state: State) -> State:
    """Granite call: draft an answer from the retrieved snippets.

    Uses ONLY the snippets per the system prompt. If retrieval came up
    empty, Granite is still invoked so the grader can fairly mark a
    "no context" answer as ungrounded and trigger a query rewrite.
    """
    formatted = _format_snippets(state["retrieved"])
    prompt = _SYNTH_PROMPT.format(
        question=state["question"],
        formatted_snippets=formatted,
    )
    answer = await _call_granite(prompt, system=_SYNTH_SYSTEM)
    return state.update(
        draft_answer=answer,
        log=[*state.get("log", []), f"Synthesized draft ({len(answer)} chars)"],
    )


@action(
    reads=["question", "draft_answer", "retrieved", "grading_attempts", "log"],
    writes=["grade_score", "grade_reason", "grading_attempts", "log"],
)
async def grade(state: State) -> State:
    """Granite call: score the draft 1-5 on grounding + relevance.

    Parse failure is treated as a bad grade (score 0) so the rewrite
    branch can still fire. Every attempt -- valid or not -- is
    appended to ``grading_attempts`` for audit.
    """
    formatted = _format_snippets(state["retrieved"])
    prompt = _GRADER_PROMPT.format(
        question=state["question"],
        draft_answer=state["draft_answer"],
        formatted_snippets=formatted,
    )
    raw = await _call_granite(prompt, system=_GRADER_SYSTEM)
    score, reason = _parse_grade(raw)
    attempts = [
        *state.get("grading_attempts", []),
        {
            "round": len(state.get("grading_attempts", [])) + 1,
            "score": score,
            "reason": reason,
            "raw_response": raw,
        },
    ]
    return state.update(
        grade_score=score,
        grade_reason=reason,
        grading_attempts=attempts,
        log=[*state.get("log", []), f"Graded round {len(attempts)}: score={score}"],
    )


@action(
    reads=["question", "search_queries", "grade_score", "grade_reason", "log"],
    writes=["search_queries", "log"],
)
async def rewrite_query(state: State) -> State:
    """Granite call: propose a better query when the grade is weak.

    Appends the (stripped) suggestion to ``search_queries`` so the
    next ``retrieve`` step picks it up via ``search_queries[-1]``.
    """
    previous = state["search_queries"][-1]
    prompt = _REWRITER_PROMPT.format(
        grade_score=state["grade_score"],
        grade_reason=state["grade_reason"] or "(no reason given)",
        question=state["question"],
        previous_query=previous,
    )
    raw = await _call_granite(prompt, system=_REWRITER_SYSTEM)
    new_query = raw.strip() or previous
    return state.update(
        search_queries=[*state["search_queries"], new_query],
        log=[
            *state.get("log", []),
            f"Rewrote query: {previous!r} -> {new_query!r}",
        ],
    )


@action(
    reads=[
        "question",
        "draft_answer",
        "grade_score",
        "grade_reason",
        "grading_attempts",
        "search_queries",
        "retrieved",
        "log",
    ],
    writes=["final_answer", "log"],
)
async def finalize(state: State) -> State:
    """Terminal: package the final answer with the audit trail.

    Citations come from the doc names of the most recent retrieval,
    which is what the final draft was actually written against.
    """
    final = {
        "question": state["question"],
        "answer": state["draft_answer"],
        "final_grade": state["grade_score"],
        "final_grade_reason": state["grade_reason"],
        "rounds_taken": len(state["grading_attempts"]),
        "search_queries_tried": list(state["search_queries"]),
        "citations": list(state["retrieved"].keys()),
    }
    return state.update(
        final_answer=final,
        log=[
            *state.get("log", []),
            f"Finalized after {final['rounds_taken']} round(s) at grade {final['final_grade']}",
        ],
    )


# ── graph ───────────────────────────────────────────────────────────


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            ask=ask,
            retrieve=retrieve,
            synthesize=synthesize,
            grade=grade,
            rewrite_query=rewrite_query,
            finalize=finalize,
        )
        .with_transitions(
            ("ask", "retrieve"),
            ("retrieve", "synthesize"),
            ("synthesize", "grade"),
            (
                "grade",
                "finalize",
                Condition.expr(
                    f"grade_score >= {_PASS_GRADE} or len(grading_attempts) >= max_rounds"
                ),
            ),
            (
                "grade",
                "rewrite_query",
                Condition.expr(
                    f"grade_score < {_PASS_GRADE} and len(grading_attempts) < max_rounds"
                ),
            ),
            ("rewrite_query", "retrieve"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            question="",
            search_queries=[],
            corpus_dir=None,
            retrieved={},
            draft_answer=None,
            grade_score=0,
            grade_reason="",
            grading_attempts=[],
            max_rounds=_DEFAULT_MAX_ROUNDS,
            final_answer=None,
            log=[],
        )
        .with_entrypoint("ask")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="adaptive-crag",
        instructions=(
            "Self-correcting RAG agent. Start a session with "
            "ask(question=..., max_rounds=3, corpus_dir=None). "
            "corpus_dir defaults to the shipped corpus at "
            "examples/data/parallel_research/ but accepts any directory "
            "(supports ~ and relative paths) whose subfolders contain "
            "markdown documents. The FSM walks ask -> retrieve "
            "(term-frequency search over the configured corpus) -> "
            "synthesize (Granite generates a 2-3 sentence answer from "
            "the retrieved snippets) -> grade (Granite scores the "
            "answer 1-5 on grounding plus relevance). On a passing "
            "grade (>=4) or at the round cap the FSM routes to "
            "finalize; otherwise rewrite_query asks Granite for a "
            "better search query and the loop returns to retrieve. "
            "Capped at 3 rounds by default. Unlike a one-shot RAG pipeline, "
            "this FSM grades its own output rather than accepting it "
            "blindly, and every grading attempt plus every search "
            "query tried is preserved in state for audit."
        ),
    )


if __name__ == "__main__":
    build_server().run()
