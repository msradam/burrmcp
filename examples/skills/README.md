# Example Skills

This directory holds verbatim third-party Claude Code skills, included
as source material for BurrMCP demos that decompose a real `SKILL.md`
into a Burr state machine of caller-LLM prompts and mount it as an MCP
server.

All content under `examples/skills/<slug>/SKILL.md` is unmodified
upstream material under its own license. See each subdirectory's
`ATTRIBUTION.md` for source URL, original author, license (SPDX), and
pull date.

## Skills included

| Slug | One-line summary | License |
| --- | --- | --- |
| `security-audit` | Audit a web app for vulnerabilities. Inside-codebase source review or outside black-box probing, with server/infra sweep and rate-limit deep-dive. | MIT |
| `claude-api` | Build, debug, and optimize Claude API / Anthropic SDK apps with prompt caching, and migrate code across Claude model versions. | Apache-2.0 |
| `mcp-builder` | Guide for creating high-quality MCP (Model Context Protocol) servers in Python (FastMCP) or Node/TypeScript (MCP SDK). | Apache-2.0 |
| `webapp-testing` | Test local web applications by writing native Python Playwright scripts, with a helper for server lifecycle management. | Apache-2.0 |
| `skill-creator` | Meta-skill for creating, editing, and evaluating other skills, including description tuning for trigger accuracy. | Apache-2.0 |

## First FSM demo

The `security-audit` skill is the nominated starting point for the
SKILL-to-FSM conversion. It has explicit ordered phases (context
detection, source-code checklist, black-box checklist, infra sweep,
rate-limit deep-dive, advisory writing) that map cleanly to Burr
states, and its branching on inside-vs-outside mode is a natural
conditional transition.

## Licenses

Each skill carries its own license terms. Do not assume any single
license covers this directory. Before redistributing or modifying a
skill, read its `ATTRIBUTION.md` and the upstream LICENSE file linked
there.
