"""Skill-to-FSM: doc co-authoring workflow as caller-LLM prompts.

The FSM is pure orchestration, with no server-side LLM calls and
no shellouts, so tests just exercise the transitions and the
prompt-emission. The agent is simulated by feeding canned artifacts
into each action's inputs and reading ``state.current_prompt``
afterward.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from doc_coauthoring import build_server

_CONTEXT_DUMP = (
    "Project is migrating off the in-house auth service onto a vendored "
    "OIDC provider. Engineering wants the spec by EOQ. Stakeholders are "
    "security (mostly satisfied), platform (concerned about latency), "
    "and product (wants timeline). Past incident: 2025-Q3 session token "
    "leak from the legacy service is what drove the rebuild."
)
_CLARIFICATIONS = [
    {
        "question": "Are we keeping the legacy session format for backwards-compat?",
        "self_answer": "No; the migration is the chance to drop it.",
        "needs_human": False,
    },
]
_SECTIONS = [
    {"name": "decision", "purpose": "what we're proposing and why", "est_words": 250},
    {"name": "alternatives", "purpose": "what we considered and rejected", "est_words": 200},
    {"name": "rollout", "purpose": "phased migration plan", "est_words": 150},
]
_DRAFT_TEXT = (
    "We propose adopting Vendor X's OIDC offering, replacing the in-house "
    "auth service over a six-week phased rollout."
)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


async def _walk_through_stage_1(client) -> None:
    await _step(
        client,
        "start_doc",
        doc_type="decision doc",
        audience="engineering leadership",
        desired_impact="approval by EOQ",
    )
    await _step(client, "gather_context", context_dump=_CONTEXT_DUMP)
    await _step(client, "confirm_context", clarifications=_CLARIFICATIONS)


async def _draft_all_sections(client) -> None:
    for s in _SECTIONS:
        await _step(
            client,
            "draft_section",
            section_name=s["name"],
            drafted_content=_DRAFT_TEXT,
        )


# == start_doc validation =============================================


@pytest.mark.asyncio
async def test_start_doc_rejects_empty_doc_type():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "start_doc",
                doc_type="",
                audience="eng leadership",
                desired_impact="approval",
            )
        )
        assert out["error"] == "action_error"
        assert "doc_type must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_start_doc_rejects_empty_audience():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "start_doc",
                doc_type="spec",
                audience="",
                desired_impact="approval",
            )
        )
        assert out["error"] == "action_error"
        assert "audience must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_start_doc_emits_stage_1_prompt_with_meta():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "start_doc",
                doc_type="decision doc",
                audience="platform team",
                desired_impact="decision by Friday",
            )
        )
        prompt = out["state"]["current_prompt"]
        assert "STAGE 1 of 3" in prompt
        assert "CONTEXT GATHERING" in prompt
        assert "decision doc" in prompt
        assert "platform team" in prompt


# == Stage 1: thin context dump refused ==============================


@pytest.mark.asyncio
async def test_gather_context_refuses_thin_dump():
    """The SKILL says don't let gaps accumulate; FSM enforces a
    substantive Stage-1 dump before exit."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_doc",
            doc_type="spec",
            audience="eng",
            desired_impact="approval",
        )
        out = _payload(await _step(client, "gather_context", context_dump="too short"))
        assert out["error"] == "action_error"
        assert "too thin" in out["error_message"]


@pytest.mark.asyncio
async def test_confirm_context_refuses_no_clarifications():
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_doc",
            doc_type="spec",
            audience="eng",
            desired_impact="approval",
        )
        await _step(client, "gather_context", context_dump=_CONTEXT_DUMP)
        out = _payload(await _step(client, "confirm_context", clarifications=[]))
        assert out["error"] == "action_error"
        assert "clarifications too short" in out["error_message"]


# == Stage 2: structure ===============================================


@pytest.mark.asyncio
async def test_agree_structure_refuses_too_few_sections():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        out = _payload(
            await _step(
                client,
                "agree_structure",
                sections=[{"name": "only", "purpose": "alone"}],
            )
        )
        assert out["error"] == "action_error"
        assert "sections too few" in out["error_message"]


@pytest.mark.asyncio
async def test_agree_structure_refuses_duplicate_names():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        out = _payload(
            await _step(
                client,
                "agree_structure",
                sections=[
                    {"name": "decision", "purpose": "what"},
                    {"name": "decision", "purpose": "dup"},
                ],
            )
        )
        assert out["error"] == "action_error"
        assert "duplicate section name" in out["error_message"]


# == Stage 2: drafting loop ===========================================


@pytest.mark.asyncio
async def test_draft_section_refuses_unknown_name():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        out = _payload(
            await _step(
                client,
                "draft_section",
                section_name="nonexistent",
                drafted_content=_DRAFT_TEXT,
            )
        )
        assert out["error"] == "action_error"
        assert "not in the agreed section list" in out["error_message"]


@pytest.mark.asyncio
async def test_draft_section_refuses_empty_content():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        out = _payload(
            await _step(
                client,
                "draft_section",
                section_name="decision",
                drafted_content="   ",
            )
        )
        assert out["error"] == "action_error"


@pytest.mark.asyncio
async def test_drafting_loop_remains_at_draft_section_until_all_done():
    """The FSM keeps draft_section as the next-action option while
    any section is still undrafted, and only opens complete_drafting
    once every agreed section has a draft."""
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        # Draft only the first section. complete_drafting must still be unreachable.
        out = _payload(
            await _step(
                client,
                "draft_section",
                section_name="decision",
                drafted_content=_DRAFT_TEXT,
            )
        )
        assert "draft_section" in out["valid_next_actions"]
        assert "complete_drafting" not in out["valid_next_actions"]
        # Draft the rest.
        await _step(
            client,
            "draft_section",
            section_name="alternatives",
            drafted_content=_DRAFT_TEXT,
        )
        out = _payload(
            await _step(
                client,
                "draft_section",
                section_name="rollout",
                drafted_content=_DRAFT_TEXT,
            )
        )
        # All sections drafted -> complete_drafting opens; the loop stays open too.
        assert "complete_drafting" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_complete_drafting_refuses_with_undrafted_sections():
    """Verified at the action-body layer, not just at the transition
    layer: even if Burr's condition machinery let the call through,
    the action raises."""
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        await _step(
            client,
            "draft_section",
            section_name="decision",
            drafted_content=_DRAFT_TEXT,
        )
        # Transition condition should refuse this as invalid_transition.
        out = _payload(
            await _step(
                client,
                "complete_drafting",
                final_review_notes="premature",
            )
        )
        assert out["error"] == "invalid_transition"


# == Stage 3: reader testing ==========================================


@pytest.mark.asyncio
async def test_reader_test_refuses_no_predicted_questions():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        await _draft_all_sections(client)
        await _step(
            client,
            "complete_drafting",
            final_review_notes="reviewed end-to-end",
        )
        out = _payload(
            await _step(
                client,
                "reader_test",
                predicted_questions=[],
                test_results=[],
            )
        )
        assert out["error"] == "action_error"
        assert "predicted_questions" in out["error_message"]


@pytest.mark.asyncio
async def test_reader_test_routes_to_finalize_when_clean():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        await _draft_all_sections(client)
        await _step(client, "complete_drafting", final_review_notes="reviewed")
        out = _payload(
            await _step(
                client,
                "reader_test",
                predicted_questions=["Why this vendor?"],
                test_results=[
                    {
                        "question": "Why this vendor?",
                        "answer_from_doc_only": "covered in decision section",
                        "doc_supports_answer": True,
                        "notes": "",
                    }
                ],
                issues_found=[],
            )
        )
        assert "error" not in out
        assert out["valid_next_actions"] == ["finalize_doc"]
        assert "FINALIZE" in out["state"]["current_prompt"]


@pytest.mark.asyncio
async def test_reader_test_routes_back_to_draft_when_blocking():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        await _draft_all_sections(client)
        await _step(client, "complete_drafting", final_review_notes="reviewed")
        out = _payload(
            await _step(
                client,
                "reader_test",
                predicted_questions=["What's the timeline?"],
                test_results=[
                    {
                        "question": "What's the timeline?",
                        "answer_from_doc_only": "unclear",
                        "doc_supports_answer": False,
                        "notes": "rollout section omits dates",
                    }
                ],
                issues_found=[
                    {
                        "section_name": "rollout",
                        "issue": "no concrete dates",
                        "severity": "blocking",
                    }
                ],
            )
        )
        assert "error" not in out
        assert out["valid_next_actions"] == ["draft_section"]
        assert "BLOCKING issues" in out["state"]["current_prompt"]


# == Stage gating: can't skip ahead ====================================


@pytest.mark.asyncio
async def test_cannot_skip_to_finalize_doc():
    """Agent can't jump from start_doc directly to finalize_doc."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_doc",
            doc_type="spec",
            audience="eng",
            desired_impact="approval",
        )
        out = _payload(await _step(client, "finalize_doc", final_doc="premature"))
        assert out["error"] == "invalid_transition"
        assert out["valid_next_actions"] == ["gather_context"]


@pytest.mark.asyncio
async def test_cannot_skip_stage_1_into_stage_2():
    """Even after start_doc, agree_structure is not reachable until
    Stage 1 (gather_context -> confirm_context) is done."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "start_doc",
            doc_type="spec",
            audience="eng",
            desired_impact="approval",
        )
        out = _payload(await _step(client, "agree_structure", sections=_SECTIONS))
        assert out["error"] == "invalid_transition"


# == Terminal artifact + history ======================================


@pytest.mark.asyncio
async def test_happy_walk_end_to_end():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        await _draft_all_sections(client)
        await _step(client, "complete_drafting", final_review_notes="reviewed end-to-end")
        await _step(
            client,
            "reader_test",
            predicted_questions=["Why now?"],
            test_results=[
                {
                    "question": "Why now?",
                    "answer_from_doc_only": "covered in decision",
                    "doc_supports_answer": True,
                    "notes": "",
                }
            ],
            issues_found=[],
        )
        out = _payload(
            await _step(
                client,
                "finalize_doc",
                final_doc="# Final doc\n\nFull text here.\n",
            )
        )
        assert "error" not in out
        summary = out["state"]["doc_summary"]
        assert summary["doc_type"] == "decision doc"
        assert summary["section_count"] == len(_SECTIONS)
        assert summary["sections_drafted"] == len(_SECTIONS)
        assert summary["reader_questions_tested"] == 1


@pytest.mark.asyncio
async def test_history_records_each_phase():
    server = build_server()
    async with Client(server) as client:
        await _walk_through_stage_1(client)
        await _step(client, "agree_structure", sections=_SECTIONS)
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == [
            "start_doc",
            "gather_context",
            "confirm_context",
            "agree_structure",
        ]
