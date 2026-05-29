# Changelog

Notable changes to Theodosia. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning.

## [Unreleased]

### Fixed (round 6: fifth exploration audit)
- **Personas reference page.** New `website/src/content/docs/personas.md`
  documents the PERSONA.md format, the `personas=` mounting shapes
  (directory / file / dict), the MCP prompt namespace
  (`theodosia/persona/<name>`), the single-brace `{state.x}` placeholder
  syntax (versus the Jinja-style `{{ ... }}` reviewers tend to reach for),
  the full placeholder table, and a runnable example. Previously the only
  mention of personas was a one-liner in `authoring.md`'s Assembly snippet.
- **`tutorial.md`** passed `build_application()` (a built `Application`)
  to `mount()`, contradicting `authoring.md` which insists on the factory
  form for per-session state isolation. All three spots fixed.
- **`theodosia status`** marked empty tracker directories as `running`
  when they had zero recorded steps. Now reports `empty` with a `∅`
  glyph, distinguishing "still running" from "never ran a step".

### Fixed (round 5: fourth exploration audit)
- **Typed inputs are now actually typed.** Pydantic-annotated action
  parameters previously received plain dicts at runtime, contradicting
  the JSON schema Theodosia advertised. `mount()` now coerces dict
  values to the declared Pydantic model before invoking the action,
  matching the `input_schemas` advertised at `theodosia://graph`. The
  `authoring.md` example with `order.model_dump()` now runs.
- **`cli.md` observability section** said sessions write to `~/.burr` by
  default; corrected to `~/.theodosia` (the path the CLI reads by
  default), with the `~/.burr` path noted as the Burr-first alternative.
  Resolves the contradiction with `authoring.md` and `observability.md`.
- **`tools.md` graph row** now mentions `input_schemas` so a reader looking
  at the resource catalog learns about typed-input discoverability.
- **`tools.md` subruns row** notes the resource only appears when the
  FSM uses `spawn_subapp`, not unconditionally.

### Fixed (round 4: third exploration audit)
- **Typed-input discoverability.** `theodosia://graph` now carries an
  `input_schemas` field per action: for Pydantic-typed inputs it surfaces
  `model_json_schema()`; for built-in types a `{type: ...}` shorthand. An
  agent reading the graph resource now sees that `take_order` requires an
  `order` parameter shaped like `{item: str, qty: int}`, so the call shape
  `step("take_order", {"order": {...}})` is reachable from the docs the
  agent can read at cold start. `authoring.md` gets a "Typed inputs"
  section with the common-trap example.
- **`next_hint` no longer truncates mid-word.** The 160-char limit on the
  embedded action_error message used a hard slice; now uses
  `_truncate_words` for a word-boundary cut with an ellipsis.
- **`theodosia primer` "Next steps" wording.** Was "Author your own graph"
  pointing at `doctor` (which validates, not authors); now reads
  "Validate a graph you authored: theodosia doctor ..." and "Mount it as
  an MCP server: theodosia serve ...".
- **`sessions.md` fork doc** clarified: the forked run gets a new
  `app_id`, but the tracker for it is written on the next `step`. Right
  after `fork_at`, `theodosia sessions show <new-app-id>` may report
  "no steps recorded yet" until you take one more step.
- **`cannot_fork_to_refusal`** documented in `sessions.md`.

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
