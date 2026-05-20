# Attribution

Source: https://raw.githubusercontent.com/trailofbits/skills/main/plugins/differential-review/skills/differential-review/SKILL.md
Original author / org: Trail of Bits (https://github.com/trailofbits)
License: CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)
Pulled on: 2026-05-20
Original filename: SKILL.md

This skill is included verbatim in the BurrMCP examples folder
to demonstrate the "SKILL-to-FSM" pattern: a real Claude Code
skill is decomposed into a Burr state machine of prompts, then
mounted as an MCP server so the order of steps becomes verifiable
at the protocol layer.

The CC BY-SA 4.0 license requires that any redistribution of the
SKILL.md content keep attribution and apply the same license to
derivative works. The verbatim SKILL.md alongside is the
redistribution; the Python FSM in `examples/differential_review.py`
is independently licensed under this repo's terms but cites the
SKILL as the source of its prompt structure and phase ordering.
