# Attribution

Source: https://github.com/trailofbits/skills/tree/main/plugins/fp-check/skills/fp-check
Original author / org: Trail of Bits (https://github.com/trailofbits)
License: CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)
Pulled on: 2026-05-20

Files in this directory pulled verbatim from upstream:

- `SKILL.md` -- the SKILL entry point
- `standard-verification.md` -- linear single-pass checklist (Steps 1-6)
- `deep-verification.md` -- task-based orchestration for complex bugs
- `bug-class-verification.md` -- class-specific verification requirements
- `false-positive-patterns.md` -- 13-item checklist + red flags
- `evidence-templates.md` -- documentation templates

This skill is included verbatim in the BurrMCP examples folder
to demonstrate the "SKILL-to-FSM" pattern: a real Claude Code
skill is decomposed into a Burr state machine of prompts, then
mounted as an MCP server so the order of steps becomes verifiable
at the protocol layer.

The six-gate criteria the FSM enforces are drawn from
`references/gate-reviews.md` in the same upstream pack.
