"""Skill-to-FSM: doc co-authoring workflow as caller-LLM prompts.

A real Anthropic SKILL decomposed into a Burr FSM whose actions emit
prompts for the *caller* LLM (Opus, Sonnet, Haiku, GPT-5, whoever is
driving BurrMCP through an MCP client). The FSM does not call any LLM
itself; it stores the caller's structured artifacts in state and gates
which prompt-phase is reachable next.

Source SKILL: ``examples/skills/doc-coauthoring/SKILL.md`` (Apache 2.0,
github.com/anthropics/skills). The three stages below are the same the
SKILL walks through: Context Gathering, Refinement & Structure, Reader
Testing.

Shape:

    start_doc(doc_type, audience, desired_impact, template=None)
      -> gather_context(context_dump, sources_consulted)
      -> confirm_context(clarifications)
      -> agree_structure(sections)
      -> draft_section(section_name, brainstorm_options, kept, drafted_content)
            (loops; the SKILL says "for each section ...")
      -> complete_drafting(final_review_notes)
      -> reader_test(predicted_questions, test_results, issues_found=[])
            (issues_found non-empty -> loops back into draft_section
            for any problematic section)
      -> finalize_doc(final_doc)   [terminal]

What the FSM gives the SKILL:

* Each phase becomes a separate visible step in ``burr://history``
  and ``burr://trace``. The doc-writing trail is the artifact: every
  prompt the caller LLM was given + every artifact it returned.
* Stage 1 (context gathering) is enforced: the agent cannot jump
  straight to drafting. The SKILL says "Don't let gaps accumulate".
* Stage 2 (per-section drafting) only opens after structure is agreed.
  The SKILL warns about premature drafting; the FSM makes it impossible.
* Stage 3 (reader testing) is mandatory. The SKILL says "test the doc
  with a fresh Claude (no context) to catch blind spots before others
  read it"; the FSM refuses ``finalize_doc`` without ``reader_test``
  having run.
* ``complete_drafting`` validates that every agreed section has been
  drafted before exiting Stage 2. Skipping a section surfaces as an
  ``action_error``, agent-recoverable.

Pure FSM. No server-side LLM calls, no shellouts. Complementary to
``security_audit.py`` (defensive engineering domain): this one is
"agent applies a writing SKILL's procedure under FSM-enforced order".

Run:

    python examples/doc_coauthoring.py
"""

from __future__ import annotations

from typing import Any

from burr.core import ApplicationBuilder, Condition, State, action
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "skill-doc-coauthoring-demo"

_MIN_CONTEXT_LEN = 80
_MIN_CLARIFICATIONS = 1
_MIN_SECTIONS = 2


# == prompt templates (drawn from the doc-coauthoring SKILL) ==========


_PROMPT_GATHER_CONTEXT = """\
You are co-authoring a `{doc_type}` for the audience: `{audience}`.
Desired impact when read: `{desired_impact}`.
Template / format reference: `{template_summary}`.

STAGE 1 of 3: CONTEXT GATHERING (info dump).

Goal: close the gap between what you (the agent) know and what the
human in the loop knows, so your guidance is informed later.

The SKILL says: "Don't worry about organizing it; just get it all
out." Pull everything you can from available context. For each of:

- Background on the project / problem
- Related team discussions or shared documents
- Why alternative solutions aren't being used
- Organizational context (team dynamics, past incidents, politics)
- Timeline pressures or constraints
- Technical architecture or dependencies
- Stakeholder concerns

... record what you know, what you observed, and what you'd need to
ask a human for. If you have access to integration tools (Slack,
filesystem, web), use them to pull in context now.

Call `gather_context(context_dump=..., sources_consulted=[...])` with:
- context_dump: a multi-paragraph string capturing the full info dump
  (must be at least {min_context_len} characters; this is a SKILL
  requirement, the FSM will refuse a thin dump as an action_error).
- sources_consulted: list of strings, each naming an integration /
  file / channel you actually consulted. Empty list is fine if you
  had no integrations.
"""


_PROMPT_CONFIRM_CONTEXT = """\
STAGE 1 of 3: CONTEXT GATHERING (clarifying questions).

Now ask {min_clarifications}+ clarifying questions about what you
just collected. The SKILL says: "Sufficient context has been gathered
when questions show understanding; when edge cases and trade-offs
can be asked about without needing basics explained."

Generate the questions, answer them yourself based on what you can
reason out from the context dump, and flag any that still need a
human in the loop.

Call `confirm_context(clarifications=[...])` with a list of dicts:
  [{{"question": "...", "self_answer": "...", "needs_human": false}},
   ...]

The FSM exits Stage 1 on this call and opens Stage 2 (structure).
"""


_PROMPT_AGREE_STRUCTURE = """\
STAGE 2 of 3: REFINEMENT AND STRUCTURE (agree the outline).

Propose the section list for this `{doc_type}`. The SKILL says:
"If document structure is clear: ask which section to start with.
If user doesn't know what sections they need: suggest 3-5 sections
appropriate for the doc type."

For a `{doc_type}` aimed at `{audience}`, propose at least
{min_sections} sections. For each section, capture:
- name: short identifier (snake_case or kebab-case)
- purpose: one sentence on what this section achieves
- est_words: rough size estimate

Pick a section ordering that puts the highest-unknowns section
first. For decision docs that's usually the core proposal; for
specs the technical approach; summary sections go last.

Call `agree_structure(sections=[...])` with a list of dicts:
  [{{"name": "...", "purpose": "...", "est_words": 200}}, ...]

The FSM locks this structure. From this point, every section in the
list must be drafted before you can call `complete_drafting`.
"""


_PROMPT_DRAFT_SECTION = """\
STAGE 2 of 3: REFINEMENT AND STRUCTURE (drafting).

Sections agreed: {sections_summary}
Sections still to draft: {remaining_summary}

For the next section, the SKILL prescribes:

1. Clarifying questions: 5-10 questions about what should be included.
2. Brainstorm: 5-20 candidate items / angles / arguments.
3. Curation: pick which to keep, remove, combine.
4. Gap check: anything important missing?
5. Drafting: write the section.
6. (Iterative refinement happens between calls; if you want to revise,
   call `draft_section` again with the same section name and updated
   content.)

Call `draft_section(section_name=..., brainstorm_options=[...],
kept_options=[...], drafted_content="...")` with:
- section_name: one of the agreed section names
  (the FSM will refuse an unknown name as an action_error).
- brainstorm_options: the candidate items you considered.
- kept_options: indices or short labels for what you kept.
- drafted_content: the actual section text (markdown).

When every agreed section has at least one draft on record, you can
move on to `complete_drafting`.
"""


_PROMPT_COMPLETE_DRAFTING = """\
STAGE 2 of 3: REFINEMENT AND STRUCTURE (final review of all sections).

All agreed sections have been drafted. The SKILL says: "As approaching
completion (80%+ of sections done), re-read the entire document and
check for flow and consistency across sections, redundancy or
contradictions, anything that feels like 'slop' or generic filler,
whether every sentence carries weight."

Do that review now. Call `complete_drafting(final_review_notes="...")`
with a short paragraph summarizing what you adjusted or what you noted
for the reader-testing pass.

The FSM exits Stage 2 on this call and opens Stage 3 (reader testing).
"""


_PROMPT_READER_TEST = """\
STAGE 3 of 3: READER TESTING.

The SKILL says: "Test the document with a fresh Claude (no context
bleed) to verify it works for readers. This catches blind spots:
things that make sense to the authors but might confuse others."

Step 1: predict 5-10 questions a real reader would ask when they
encounter this doc cold.

Step 2: for each question, simulate answering it using ONLY the doc
text (not the full context dump from Stage 1). Note where the doc
fails to support the answer, requires assumed knowledge, or is
ambiguous.

Step 3: additional checks: ambiguity, false assumptions, internal
contradictions.

Step 4: list any issues that require fixing.

Call `reader_test(predicted_questions=[...], test_results=[...],
issues_found=[...])` with:
- predicted_questions: list of strings (the reader questions).
- test_results: list of dicts, one per question:
    {{"question": "...", "answer_from_doc_only": "...",
      "doc_supports_answer": true|false, "notes": "..."}}
- issues_found: list of dicts:
    {{"section_name": "...", "issue": "...",
      "severity": "blocking|nice-to-have"}}

If issues_found contains BLOCKING items, the FSM will route you back
to draft_section for revision. If it's empty or non-blocking only,
you can call `finalize_doc`.
"""


_PROMPT_FINALIZE = """\
STAGE 3 of 3: FINALIZE.

Reader testing complete with no blocking issues. Compile the final
document. Use this shape:

# {doc_type}: <title>

> Audience: {audience}
> Impact: {desired_impact}

<sections in agreed order>

---
Process notes (optional appendix):
- Stage 1 context dump: <one-line summary>
- Stage 3 reader-testing pass: <brief summary>

Call `finalize_doc(final_doc="...")` with the full markdown.
The FSM terminates here.
"""


# == actions (each emits a prompt; no server-side LLM call) ===========


@action(
    reads=[],
    writes=[
        "doc_type",
        "audience",
        "desired_impact",
        "template_summary",
        "context_dump",
        "sources_consulted",
        "clarifications",
        "sections",
        "section_drafts",
        "final_review_notes",
        "reader_questions",
        "reader_results",
        "reader_issues",
        "final_doc",
        "current_prompt",
        "log",
    ],
)
async def start_doc(
    state: State,
    doc_type: str,
    audience: str,
    desired_impact: str,
    template: str | None = None,
) -> State:
    """Start the doc workflow. Captures the SKILL's meta-context
    (doc type, audience, desired impact, optional template reference)
    and emits the Stage-1 context-dump prompt.

    Args:
        doc_type: What kind of doc this is. E.g. "decision doc",
            "technical spec", "PRD", "incident postmortem", "RFC".
        audience: Who reads it. E.g. "engineering leadership",
            "the platform team", "external developers using our API".
        desired_impact: What changes when someone reads it. E.g.
            "decision approved by EOW", "spec implementable without
            further clarification", "new hires can onboard in <2 days".
        template: Optional. Pointer or summary of a template/format
            to follow. The SKILL says to ask about this up front.
    """
    if not doc_type.strip():
        raise ValueError("doc_type must not be empty")
    if not audience.strip():
        raise ValueError("audience must not be empty")
    if not desired_impact.strip():
        raise ValueError("desired_impact must not be empty")
    template_summary = (template or "").strip() or "(no template specified)"
    prompt = _PROMPT_GATHER_CONTEXT.format(
        doc_type=doc_type,
        audience=audience,
        desired_impact=desired_impact,
        template_summary=template_summary,
        min_context_len=_MIN_CONTEXT_LEN,
    )
    return state.update(
        doc_type=doc_type,
        audience=audience,
        desired_impact=desired_impact,
        template_summary=template_summary,
        context_dump="",
        sources_consulted=[],
        clarifications=[],
        sections=[],
        section_drafts={},
        final_review_notes="",
        reader_questions=[],
        reader_results=[],
        reader_issues=[],
        final_doc=None,
        current_prompt=prompt,
        log=[f"Doc started: type={doc_type!r}, audience={audience!r}"],
    )


@action(
    reads=["log"],
    writes=["context_dump", "sources_consulted", "current_prompt", "log"],
)
async def gather_context(
    state: State,
    context_dump: str,
    sources_consulted: list[str] | None = None,
) -> State:
    """Stash the Stage-1 info dump. Refuses thin dumps as action_error
    so the agent cannot speed-run past context gathering.
    """
    if len(context_dump.strip()) < _MIN_CONTEXT_LEN:
        raise ValueError(
            f"context_dump is too thin ({len(context_dump.strip())} chars; "
            f"SKILL requires substantive dump >= {_MIN_CONTEXT_LEN}). "
            "The SKILL says: 'Don't let gaps accumulate; address them as "
            "they come up.' Re-call with a fuller dump."
        )
    return state.update(
        context_dump=context_dump,
        sources_consulted=list(sources_consulted or []),
        current_prompt=_PROMPT_CONFIRM_CONTEXT.format(min_clarifications=_MIN_CLARIFICATIONS),
        log=[
            *state["log"],
            f"Context gathered: {len(context_dump)} chars, "
            f"{len(sources_consulted or [])} source(s)",
        ],
    )


@action(
    reads=["doc_type", "audience", "log"],
    writes=["clarifications", "current_prompt", "log"],
)
async def confirm_context(
    state: State,
    clarifications: list[dict[str, Any]],
) -> State:
    """Stash clarifying-question artifacts and gate the exit from
    Stage 1. The SKILL says context is sufficient when "edge cases
    and trade-offs can be asked about without needing basics
    explained"; the FSM proxy for that is a minimum number of
    structured clarifications.
    """
    items = list(clarifications or [])
    if len(items) < _MIN_CLARIFICATIONS:
        raise ValueError(
            f"clarifications too short ({len(items)} < "
            f"{_MIN_CLARIFICATIONS}). The SKILL gates Stage 1 exit on "
            "demonstrated understanding; add at least one clarifying "
            "question with a self_answer."
        )
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "question" not in item or "self_answer" not in item:
            raise ValueError(
                f"clarifications[{i}] must be a dict with keys "
                "'question' and 'self_answer'; got: " + repr(item)
            )
    prompt = _PROMPT_AGREE_STRUCTURE.format(
        doc_type=state["doc_type"],
        audience=state["audience"],
        min_sections=_MIN_SECTIONS,
    )
    return state.update(
        clarifications=items,
        current_prompt=prompt,
        log=[*state["log"], f"Context confirmed: {len(items)} clarification(s)"],
    )


@action(
    reads=["log"],
    writes=["sections", "section_drafts", "current_prompt", "log"],
)
async def agree_structure(
    state: State,
    sections: list[dict[str, Any]],
) -> State:
    """Lock the section list for Stage 2. The FSM remembers these
    names so ``draft_section`` can refuse unknown names and
    ``complete_drafting`` can refuse premature exit.
    """
    items = list(sections or [])
    if len(items) < _MIN_SECTIONS:
        raise ValueError(
            f"sections too few ({len(items)} < {_MIN_SECTIONS}). The "
            "SKILL says to propose 3-5 sections appropriate for the doc "
            f"type; provide at least {_MIN_SECTIONS}."
        )
    names: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "name" not in item or "purpose" not in item:
            raise ValueError(
                f"sections[{i}] must be a dict with 'name' and 'purpose'; got: {item!r}"
            )
        name = str(item["name"]).strip()
        if not name:
            raise ValueError(f"sections[{i}].name must be a non-empty string")
        if name in names:
            raise ValueError(f"duplicate section name: {name!r}")
        names.append(name)
    summary_lines = "\n  ".join(f"- {s['name']}: {s['purpose']}" for s in items)
    prompt = _PROMPT_DRAFT_SECTION.format(
        sections_summary="\n  " + summary_lines,
        remaining_summary="\n  " + summary_lines,
    )
    return state.update(
        sections=items,
        section_drafts=dict.fromkeys(names),
        current_prompt=prompt,
        log=[*state["log"], f"Structure agreed: {len(items)} section(s) ({names})"],
    )


@action(
    reads=["sections", "section_drafts", "log"],
    writes=["section_drafts", "current_prompt", "log"],
)
async def draft_section(
    state: State,
    section_name: str,
    drafted_content: str,
    brainstorm_options: list[Any] | None = None,
    kept_options: list[Any] | None = None,
) -> State:
    """Record one section's draft. Loop-able: call again with the
    same ``section_name`` to revise; call with a new name to advance.

    Refuses ``section_name`` not in the agreed list as action_error.
    """
    drafts: dict[str, Any] = dict(state["section_drafts"])
    if section_name not in drafts:
        valid = sorted(drafts.keys())
        raise ValueError(
            f"section_name={section_name!r} is not in the agreed "
            f"section list. Valid names: {valid}. Use one of those, or "
            "go back and revise the structure."
        )
    if not drafted_content.strip():
        raise ValueError(
            f"drafted_content must not be empty for section "
            f"{section_name!r}; the SKILL requires actual section text."
        )
    drafts[section_name] = {
        "drafted_content": drafted_content,
        "brainstorm_options": list(brainstorm_options or []),
        "kept_options": list(kept_options or []),
    }
    drafted_names = [n for n, d in drafts.items() if d is not None]
    remaining_names = [n for n, d in drafts.items() if d is None]
    sections_summary = "\n  " + "\n  ".join(
        f"- {s['name']}: {'DRAFTED' if drafts[s['name']] else 'pending'}" for s in state["sections"]
    )
    if remaining_names:
        remaining_summary = "\n  " + "\n  ".join(f"- {n}" for n in remaining_names)
    else:
        remaining_summary = " (none, every section has a draft; you can call complete_drafting)"
    prompt = _PROMPT_DRAFT_SECTION.format(
        sections_summary=sections_summary,
        remaining_summary=remaining_summary,
    )
    return state.update(
        section_drafts=drafts,
        current_prompt=prompt,
        log=[
            *state["log"],
            f"Section drafted: {section_name!r} ({len(drafted_names)}/{len(drafts)} done)",
        ],
    )


@action(
    reads=["section_drafts", "log"],
    writes=["final_review_notes", "current_prompt", "log"],
)
async def complete_drafting(
    state: State,
    final_review_notes: str,
) -> State:
    """Gate the Stage 2 exit. Refuses if any agreed section is still
    undrafted. The SKILL warns about generic filler and missed
    sections; the FSM makes "all agreed sections have a draft" a
    structural invariant before reader testing can start.
    """
    drafts = dict(state["section_drafts"])
    missing = [n for n, d in drafts.items() if d is None]
    if missing:
        raise ValueError(
            f"complete_drafting refused: sections still without a draft: "
            f"{missing}. Call draft_section for each before completing."
        )
    if not final_review_notes.strip():
        raise ValueError(
            "final_review_notes must not be empty; the SKILL prescribes "
            "an end-of-Stage-2 review pass for flow, redundancy, and "
            "filler. Record what you adjusted or noted."
        )
    return state.update(
        final_review_notes=final_review_notes,
        current_prompt=_PROMPT_READER_TEST,
        log=[
            *state["log"],
            f"Drafting complete: {len(drafts)} section(s), "
            f"final review {len(final_review_notes)} chars",
        ],
    )


@action(
    reads=["sections", "section_drafts", "log"],
    writes=[
        "reader_questions",
        "reader_results",
        "reader_issues",
        "has_blocking_issues",
        "current_prompt",
        "log",
    ],
)
async def reader_test(
    state: State,
    predicted_questions: list[str],
    test_results: list[dict[str, Any]],
    issues_found: list[dict[str, Any]] | None = None,
) -> State:
    """Run the Stage 3 reader-testing artifact submission. If any
    issue is severity=='blocking', the FSM routes back into
    ``draft_section`` for the affected section. Otherwise it opens
    the path to ``finalize_doc``.
    """
    if len(predicted_questions or []) < 1:
        raise ValueError(
            "predicted_questions must contain at least one question; "
            "the SKILL requires actual reader testing, not a skip."
        )
    if len(test_results or []) < 1:
        raise ValueError(
            "test_results must contain at least one entry; you cannot "
            "pass reader testing without simulating any reader answer."
        )
    issues = list(issues_found or [])
    blocking = [i for i in issues if str(i.get("severity", "")).lower() == "blocking"]
    sections_summary = "\n  " + "\n  ".join(f"- {s['name']}: drafted" for s in state["sections"])
    if blocking:
        next_prompt = (
            "Reader test surfaced BLOCKING issues. Revise the affected "
            "sections by calling draft_section with updated content. "
            "Blocking issues:\n  "
            + "\n  ".join(
                f"- {i.get('section_name', '?')}: {i.get('issue', '?')}" for i in blocking
            )
            + "\n\nSections in scope:"
            + sections_summary
        )
    else:
        next_prompt = _PROMPT_FINALIZE.format(
            doc_type=state.get("doc_type", "doc"),
            audience=state.get("audience", "(audience)"),
            desired_impact=state.get("desired_impact", "(impact)"),
        )
    return state.update(
        reader_questions=list(predicted_questions),
        reader_results=list(test_results),
        reader_issues=issues,
        has_blocking_issues=bool(blocking),
        current_prompt=next_prompt,
        log=[
            *state["log"],
            f"Reader test: {len(predicted_questions)} question(s), "
            f"{len(issues)} issue(s) ({len(blocking)} blocking)",
        ],
    )


@action(
    reads=[
        "doc_type",
        "audience",
        "desired_impact",
        "sections",
        "section_drafts",
        "reader_issues",
        "log",
    ],
    writes=["final_doc", "doc_summary", "current_prompt", "log"],
)
async def finalize_doc(state: State, final_doc: str) -> State:
    """Terminal. Refuses if Stage 3 left any blocking issues open."""
    if not final_doc.strip():
        raise ValueError("final_doc must not be empty")
    blocking_open = [
        i for i in state["reader_issues"] if str(i.get("severity", "")).lower() == "blocking"
    ]
    if blocking_open:
        raise ValueError(
            f"finalize_doc refused: reader_test surfaced blocking issues "
            f"that were not addressed (the FSM expects you to re-call "
            f"draft_section then reader_test again). Open blocking "
            f"issues: {blocking_open}"
        )
    summary = {
        "doc_type": state["doc_type"],
        "audience": state["audience"],
        "desired_impact": state["desired_impact"],
        "section_count": len(state["sections"]),
        "sections_drafted": sum(1 for d in state["section_drafts"].values() if d is not None),
        "reader_questions_tested": len(state.get("reader_questions") or []),
        "final_doc_chars": len(final_doc),
    }
    return state.update(
        final_doc=final_doc,
        doc_summary=summary,
        current_prompt="Doc complete. Final text in state.final_doc.",
        log=[*state["log"], f"Doc finalized ({len(final_doc)} chars). Workflow complete."],
    )


# == graph ============================================================


_SECTIONS_REMAIN = Condition.expr("None in section_drafts.values()")
_ALL_SECTIONS_DRAFTED = Condition.expr("None not in section_drafts.values()")

_HAS_BLOCKING_ISSUES = Condition.expr("has_blocking_issues")
_NO_BLOCKING_ISSUES = Condition.expr("not has_blocking_issues")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_doc=start_doc,
            gather_context=gather_context,
            confirm_context=confirm_context,
            agree_structure=agree_structure,
            draft_section=draft_section,
            complete_drafting=complete_drafting,
            reader_test=reader_test,
            finalize_doc=finalize_doc,
        )
        .with_transitions(
            ("start_doc", "gather_context"),
            ("gather_context", "confirm_context"),
            ("confirm_context", "agree_structure"),
            ("agree_structure", "draft_section"),
            # Loop: keep drafting (or revising) sections until all are done.
            ("draft_section", "draft_section", _SECTIONS_REMAIN),
            ("draft_section", "complete_drafting", _ALL_SECTIONS_DRAFTED),
            ("complete_drafting", "reader_test"),
            # Reader test routes back to drafting if blocking issues exist.
            ("reader_test", "draft_section", _HAS_BLOCKING_ISSUES),
            ("reader_test", "finalize_doc", _NO_BLOCKING_ISSUES),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            doc_type="",
            audience="",
            desired_impact="",
            template_summary="",
            context_dump="",
            sources_consulted=[],
            clarifications=[],
            sections=[],
            section_drafts={},
            final_review_notes="",
            reader_questions=[],
            reader_results=[],
            reader_issues=[],
            has_blocking_issues=False,
            final_doc=None,
            doc_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_doc")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="skill-doc-coauthoring",
        instructions=(
            "Doc co-authoring via a real Anthropic SKILL decomposed "
            "into an FSM of prompts. The CALLER LLM (whoever is "
            "driving you through MCP) does the thinking; this FSM "
            "emits one structured prompt per phase and stores your "
            "artifacts. Start with start_doc(doc_type, audience, "
            "desired_impact, template=None). The three SKILL stages "
            "are enforced: Stage 1 (context: gather_context -> "
            "confirm_context) -> Stage 2 (structure: agree_structure "
            "-> draft_section loop -> complete_drafting) -> Stage 3 "
            "(reader_test -> finalize_doc). Skipping a stage, "
            "drafting a section not in the agreed structure, or "
            "finalizing with blocking reader-test issues all surface "
            "as structured refusals. Read burr://state after every "
            "step for state.current_prompt; burr://history for the "
            "full trail. Source SKILL at examples/skills/doc-coauthoring/."
        ),
    )


if __name__ == "__main__":
    build_server().run()
