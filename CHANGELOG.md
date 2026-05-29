# Changelog

Notable changes to Theodosia. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning.

## [Unreleased]

### Fixed (round 3: second exploration audit)
- **`theodosia sessions show` state diff was off by one.** Burr records
  pre-step state for sync action bodies via `post_run_step`, so the on-disk
  tracker carries each row's pre-state, not its post-state. The CLI now
  detects this per-row via `__PRIOR_STEP` and scans forward for the entry
  whose `__PRIOR_STEP` names the row's action. That entry's state is the
  true post-step state. Async action bodies (which record correctly) are
  detected the same way and used as-is, so the fix does not regress them.
  The terminal action remains stale because no forward entry exists; this
  is a Burr-tracker limit. Regression test in `tests/test_cli_sessions.py`.
- **`call_upstream` to a stdio subprocess from an in-memory client** now
  raises a clear `UpstreamError` explaining the in-memory-transport-has-no-fd
  reason and pointing at `FakeUpstream` for tests. Previously the user saw
  `RuntimeError: Client failed to connect: fileno` with no path forward.
  Doc note added to `upstream.md`.

### Fixed (round 2: exploration audit)
- **Personas were unreachable from MCP clients.** `get_prompt` returned
  `Missing required arguments: {'ctx'}` because the persona handler
  declared `ctx` without the `Context` type annotation FastMCP needs to
  recognize a server-injected parameter. The annotation now lands and
  `_build_persona_frame` reads `entry.application` (the actual attribute)
  instead of `entry.app`. Frame-aware interpolation works end-to-end.
- **Burr's "Oh no an error!" panel + Python traceback** no longer print
  on every `action_error` refusal. The action exception is captured into
  a structured wire response; the developer-facing terminal stays clean.
  Set `THEODOSIA_VERBOSE=1` to restore the old behavior.
- **FastMCP DEBUG "Sending INFO to client" notifications** are now silenced
  in `mount()`. Same `THEODOSIA_VERBOSE=1` escape hatch.
- **`Assembly.to_yaml(path=None)`** added so the YAML round-trip is
  symmetric with `from_yaml`. Returns the YAML text; writes to ``path``
  if given.
- **`theodosia primer` "Next steps"** now says `module:build_application`
  rather than `module:build`, matching the `authoring.md` convention.
- **Tracker home doc conflict resolved.** `observability.md` now leads
  with `theodosia.tracker(project=...)` (writes to `~/.theodosia`, what
  the CLI reads by default) and mentions Burr's `LocalTrackingClient`
  with its `~/.burr` path as the alternative for Burr-first projects.

### Fixed
- `theodosia primer`: self-contains the coffee-order FSM so the command
  works after a wheel install. Previously failed with "bundled coffee_order
  example not found" because the `examples/` directory is not packaged.
- `theodosia serve` now accepts `--transport http|sse|streamable-http`,
  `--host`, and `--port` flags. Previously the only available transport was
  stdio, which is not what most clients expect from a deployed server.
- README: clarified that `mount(factory)` (a callable returning an
  `Application`) is the recommended shape for per-session isolation, matching
  authoring.md. The four-tool surface description now notes the two extra
  tools (`list_resources`, `read_resource`) FastMCP's `ResourcesAsTools`
  transform adds. The structured-refusal list now includes the fork refusals
  (`cannot_fork_to_refusal`, `unknown_past_run`, `no_tracker`).
- `theodosia ui` README hint now reflects the actual fallback: auto-bootstrap
  via `uvx`, or install `theodosia[ui]`.

### Documented
- `theodosia.testing.FakeUpstream`: full usage section in `upstream.md` with
  a runnable example, plus a mention of `RecordingUpstream` and
  `ReplayingUpstream` for trajectory tests.

### Added
- `theodosia.Assembly`: a frozen-dataclass bundle of a workflow plus its
  personas, upstream config, instructions, and metadata. `Assembly.serve()`
  mounts it; `mount(assembly)` is equivalent. `from_yaml` and `from_dict`
  support declarative configuration.
- `theodosia primer`: a CLI subcommand and offline first-touch. Walks the
  bundled coffee-order FSM through the `step` tool in-process via FastMCP's
  in-memory client, prints the timeline with state diffs, and ends with one
  structured refusal so the recoverable shape is visible. No API key, no
  LLM, byte-deterministic.
- `py.typed` marker so downstream type-checkers consume Theodosia's
  annotations.
- README: "Primitives at a glance" enumeration and a "What this is not"
  scope-fencing section.

### Changed
- PyPI metadata: `[project.urls]` (Homepage, Repository, Documentation,
  Issues, Changelog), `keywords`, additional classifiers
  (`Operating System :: OS Independent`,
  `Topic :: Scientific/Engineering :: Artificial Intelligence`,
  `Topic :: Software Development :: Libraries`, `Typing :: Typed`),
  and `license-files = ["LICENSE", "NOTICE.md"]` per PEP 639.
- Trimmed verbose docstrings on `theodosia.upstream`,
  `theodosia.testing`, `theodosia._recording`, and `theodosia.persona`
  to terse declarative statements.

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
