# Changelog

Notable changes to Theodosia. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning.

## [Unreleased]

## [0.2.0] - 2026-05-25

### Added
- Tamper-evident audit ledger: every step and refusal is hash-chained into a
  `ledger.jsonl` next to the session's tracker log. `theodosia verify` recomputes
  the chain and names the exact line if any entry was altered, reordered, or
  deleted. `HashChainedLedger` and `verify_ledger` are public. The chain proves
  integrity, not confidentiality or origin.
- `unknown_action` refusals now carry the same steering fields as
  `invalid_transition` (`valid_next_actions`, `message`, `next_hint`), so a model
  that hallucinates an action name can recover from the response alone.
- Continuous integration: lint (ruff), type-check (mypy), and the test suite run
  on every push and pull request across Python 3.11 to 3.13.
- Security scanning: bandit (SAST), pip-audit (dependency vulnerabilities), and
  CodeQL, plus an OpenSSF Scorecard workflow.
- A CycloneDX SBOM is built and attached to each release.
- Dependabot for Python and GitHub Actions updates.
- A documented security model for the agent trust boundary.

## [0.1.0] - 2026-05-24

### Added
- `mount()` serves a Burr `Application` as an MCP server in STEP mode: the
  four-tool surface (`step`, `reset_session`, `fork_at`, `fork_from_past`).
- Structured refusals (`invalid_transition`, `unknown_action`,
  `validation_failed`, `action_timeout`, `action_error`) carrying
  `valid_next_actions` so a client can recover from a single error.
- `theodosia://` resources: graph, state, next, history, subruns, trace, session.
- CLI: `serve`, `doctor`, `render`, `sessions`, `watch`, `logs`, `ui`, and
  `build_cli` for rebranded downstream commands.
- `upstream`: actions reaching tools on other MCP servers via `call_upstream`.
- Input-coercion middleware for clients that send JSON-string arguments.
- Released to PyPI via Trusted Publishing (OIDC), with build attestations.
