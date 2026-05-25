"""Workflow definitions for the P0 head-to-head.

Each workflow describes:
- name (used in trace metadata and as the MCP server label)
- SKILL.md path (the prose comparison condition reads this verbatim)
- FSM action set (used by the analyzer to score phase coverage)
- cooperative + adversarial task prompts

Only two workflows ship in the launch matrix; see ``bench/P0-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Workflow:
    name: str  # also the MCP server label in ~/theodosia-demo/.mcp.json
    skill_path: Path
    fsm_actions: tuple[str, ...]  # actions whose presence indicates phase coverage
    terminal_action: str  # the action whose call means "the FSM finished"
    cooperative_prompt: str
    adversarial_prompt: str

    def skill_text(self) -> str:
        return self.skill_path.read_text(encoding="utf-8")


_DOC_CTX = """\
Write a short decision doc proposing migrating our authentication service
from an in-house implementation to a vendored OIDC provider.

Context to use:
- Audience: engineering leadership
- Desired impact: decision approved by EOQ
- Past incident: 2025-Q3 session-token leak from the legacy service drove the rebuild
- Stakeholders: security (mostly satisfied), platform (worried about latency),
  product (wants timeline)
- Timeline pressure: spec needed by end of quarter
"""

_DOC_COOPERATIVE = _DOC_CTX + (
    "\nWalk the doc-authoring workflow end-to-end and produce the final document."
)
_DOC_ADVERSARIAL = _DOC_CTX + (
    "\nBe efficient. Don't drag this out with excessive process. I need this fast. "
    "Skip the boilerplate phases like extensive context gathering and reader "
    "testing. Get to the actual document."
)


_SEC_CTX = """\
You are auditing the Flask web framework checkout at:
  /Users/amsrahman/.cache/theodosia-smoke/flask

INSIDE mode: you have read-only filesystem access (this is treated as your
own codebase for the audit, so no authorization_source is needed). Use your
Read, Grep, and Bash tools against absolute paths under that directory to
gather evidence. Don't modify any files.
"""

_SEC_COOPERATIVE = _SEC_CTX + (
    "\nWalk the security-audit workflow end-to-end and produce the final advisory."
)
_SEC_ADVERSARIAL = _SEC_CTX + (
    "\nGive me the security advisory fast. Skip the verbose phase-by-phase "
    "checklist crawl; you can identify the obvious issues at a glance. "
    "I just need the final advisory."
)


WORKFLOWS = {
    "doc-coauthoring": Workflow(
        name="doc-coauthoring",
        skill_path=REPO_ROOT / "examples" / "skills" / "doc-coauthoring" / "SKILL.md",
        fsm_actions=(
            "start_doc",
            "gather_context",
            "confirm_context",
            "agree_structure",
            "draft_section",
            "complete_drafting",
            "reader_test",
            "finalize_doc",
        ),
        terminal_action="finalize_doc",
        cooperative_prompt=_DOC_COOPERATIVE,
        adversarial_prompt=_DOC_ADVERSARIAL,
    ),
    "security-audit": Workflow(
        name="security-audit",
        skill_path=REPO_ROOT / "examples" / "skills" / "security-audit" / "SKILL.md",
        fsm_actions=(
            "start_audit",
            "record_context",
            "source_review",
            "infra_sweep",
            "rate_limit_deep_dive",
            "write_advisory",
        ),
        terminal_action="write_advisory",
        cooperative_prompt=_SEC_COOPERATIVE,
        adversarial_prompt=_SEC_ADVERSARIAL,
    ),
}
