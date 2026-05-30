# Changelog

Notable changes to Theodosia. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning.

## [Unreleased]

_Nothing yet._

## [0.4.1] - 2026-05-30

Patch release addressing reliability and correctness findings from a code
review of v0.4.0. No API breakage; same surface, tightened internals.

### Fixed

- `_resolve_key` now raises `ValueError` on a non-hex
  `THEODOSIA_LEDGER_KEY` instead of falling back to ASCII bytes. A typo in
  the hex key used to produce a different chain than the operator
  intended.
- `_append_ledger` replaces `contextlib.suppress(OSError)` (silent drop on
  disk-full / permission-denied) with WARNING-level logging on the
  `theodosia` logger. The step that ran on the wire is never blocked by
  an audit-log write failure, but the failure is visible to operators.
- `_warn_unkeyed_once` no longer flips the sentinel in the keyed branch,
  so a runtime env-var unset still emits the first warning at the next
  write. Wrapped in a `threading.Lock`.
- `load_personas` isolates per-file errors so one malformed PERSONA.md no
  longer takes down the whole mount; bad files are logged and skipped.
- `doctor`'s `_check_ledger_key_mode` only runs when the resolved
  application has a tracker wired. Without one, the ledger code path is a
  no-op, so the warning would have been a false positive on every check.
- Stale docstring claims about `allow_runtime_swap` and a `set_persona`
  tool that did not ship in v0.4.0 removed from `persona.py` and
  `adapter.py`.

### Added

- `HashChainedLedger.__init__` accepts an optional `last_hash` so a
  single-process repeat appender skips the O(n) file re-read between
  writes. `append` keeps the cache consistent.
- Process-local last-hash cache in `_ledger.py` keyed by ledger path,
  guarded by a `threading.Lock`. Same-process same-path appenders now
  O(1) per append instead of O(n^2) over a session.
- PERSONA.md trust model spelled out in the module docstring: persona
  files are trusted code; `{state.x}` placeholders can read any state
  field the FSM has written. Only mount directories you author or audit.

## [0.4.0] - 2026-05-30

v0.4 rebrand: the site stands on what Theodosia is. Every competitor
mention, defensive carve-out, and slogan tagline removed. The wire shape
gets a consistent brand glyph. The ledger story gets honest framing.

### Added

- `Persona`: PERSONA.md identity layer mounted as MCP prompts.
  Frame-aware placeholder interpolation (`{state.x}`, `{action.name}`)
  against live session state.
- `theodosia.tokens`: single-source design tokens shared by the docs CSS,
  the Burr UI overlay, and the TUI palette.
- Brand glyphs on every wire response. Success headlines use `⊢`;
  refusals use `×`. Consistent across `step`, the CLI sessions table, the
  TUI, and the audit-log surface.
- `_ledger` emits one startup WARNING when running unkeyed (SHA-only),
  naming what the chain does and does not catch. `theodosia doctor`
  surfaces the same finding statically.
- New example: `examples/the_proposal.py` (Chekhov one-act, two
  terminals, no LLM, no network).

### Changed

- `pyproject` Documentation URL points at the docs site instead of the
  GitHub README.
- README rewritten in the peer-Python-library register (Burr, FastMCP,
  Pydantic, Typer, FastAPI). Dropped "Why this shape works" defensive
  section, "What this is not" five-bullet list, "Compose with Philip"
  cross-promotion section, "Agents built with Theodosia" cross-promotion
  table.
- Site landing rewritten: comparison table, "From Statewright" doc, and
  landscape competitor roll-call deleted. Hero descriptor rewritten as a
  declarative sentence. Tagline removed.
- `architecture.md` no longer names IBM Bob or uses "monkey-patches"
  framing; rewrote "The graph is the contract" heading to "Graph
  topology" and "The action-selection trick" to "Action selection".
- `cli.md` softens "any MCP client" with a pointer to the verified
  configs and the compatibility page.
- `security-model.md` lede now describes the trust boundary directly
  instead of stating the point of Theodosia.
- `case-study.md` title changed from "the rails make the agent finish"
  to "Kimi K2.6 on o11y-bench, free-ranging vs gated".

### Removed

- `from-statewright.md` doc and all sidebar/header references to it.
- Em dashes across all authored prose (CLAUDE.md rule).

## [0.3.0] - 2026-05-29

This release closes the gap between "code exists and tests pass" and
"someone could ship this in production today". It is anchored in three
parallel dogfood runs that built real things in clean containers
(multi-tenant SaaS ticketing, an upstream MCP composition against
`@modelcontextprotocol/server-filesystem`, and a new-user-from-docs
walkthrough). One load-bearing bug was found and fixed; the rest is
onboarding, surface ergonomics, and verified docs.

### Fixed (`mount(upstream={...})` happy-path config from a running server)

`mount(upstream={"name": {"command": "...", "args": [...]}})` was dead on
arrival in any context where the parent server actually runs. FastMCP
wraps `sys.stderr` in a `StringIO` for protocol cleanliness inside a
running server, the upstream subprocess opener (`mcp.client.stdio`)
called `.fileno()` on inherited stderr, and `StringIO` has no
`.fileno()`. Subprocess crashed; the resulting `UpstreamError` blamed
"in-memory FastMCP Client", which was the wrong diagnosis.

- `_as_transport` now defaults `log_file=sys.__stderr__` on the
  `StdioTransport` it builds. `sys.__stderr__` is the original,
  unwrapped interpreter-level stderr and has a real `.fileno()`
  regardless of what's been done to `sys.stderr`.
- Users can override per-config via `{"log_file": Path(...)}` or
  any `TextIO`.
- The "fileno" `UpstreamError` message now names the actual condition
  (stderr has no real fd) and points at the override path.
- 4 regression tests in `tests/test_upstream_stderr_default.py`,
  including a `monkeypatch`ed `sys.stderr` wrap that simulates
  FastMCP's behavior.

Surfaced by a dogfood run building a real log-triage FSM against
`@modelcontextprotocol/server-filesystem`. The documented happy-path
config now works from inside a running server without a custom
`UpstreamManager`.

### Added (install hygiene: `theodosia.__version__` + namespace cleanup)

- `theodosia.__version__` resolves via `importlib.metadata` with a
  graceful `0+unknown` fallback for editable-install edge cases.
- The public namespace no longer leaks stdlib + typing names. `Any`,
  `contextlib`, and `PackageNotFoundError` previously surfaced via
  `dir(theodosia)`; moved inside their consumers.

### Added (curated examples front door)

`examples/` had 49 demos across nine pattern groups, which was
overwhelming on first visit. Two new docs solve "where do I start"
without moving files:

- `examples/CURATED.md`: six demos covering Theodosia's breadth without
  overlap, each with what it shows + the one-liner to run it. The
  six: `coffee_order`, `incident_response`, `differential_review`,
  `granite_oncall`, `sqlite_persister`, `multi_graph`. Plus a
  "promote next" section naming five more.
- `examples/README.md`: catalog front door linking CURATED.md as
  "start here", then grouping all 49 demos by pattern category.

### Added (persist-and-resume idiom in `examples/sqlite_persister.py`)

`examples/sqlite_persister.py` previously showed the *saver* hook only
(`with_state_persister(persister)`). Building a real save-and-resume
loop requires also calling Burr's `initialize_from(persister,
resume_at_next_action=True, default_state=..., default_entrypoint=...)`
as the *loader*. A dogfood agent building a real multi-tenant SaaS had
to read Burr source to discover this split.

- New `build_application_with_resume(db_path, app_id)` factory
  demonstrates the loader path side by side with the existing save-only
  factory.
- Extended docstring spells out which primitive covers which phase.
- 2 new tests in `tests/test_sqlite_persister.py` cover the new
  factory's defaults-fallback and resume-from-prior-state behavior.

### Added (Burr UI deep links from CLI)

- `theodosia sessions show <id>` prints a clickable Burr UI URL under
  the steps table (`http://localhost:7241/project/<project>/<partition_key>/<app_id>`),
  rendered as a rich link so terminals that honor OSC 8 will ⌘-click
  into the replay. `--open` opens it in the default browser.
- `theodosia status` prints the Burr UI root URL at the bottom.
- Both honor `BURR_UI_HOST` and `BURR_UI_PORT` env overrides for
  users running the UI behind a tunnel or on a non-default port.
- `--json` output for both commands carries the URL under
  `burr_ui_url`.

### Added (`mount(hooks=[...])` and `mount(middleware=[...])` kwargs)

Surface Burr's lifecycle adapter ecosystem and FastMCP's middleware
chain through the `mount()` API without making the caller reach into
the underlying objects.

- `mount(..., hooks=[hook1, hook2])` attaches Burr `LifecycleAdapter`
  instances (`PreRunStepHook`, `PostRunStepHook`, `PreStartStreamHook`,
  persister hooks, etc.) to every session's Application via Burr's
  public `LifecycleAdapterSet.with_new_adapters` API. Same surface as
  `ApplicationBuilder.with_hooks(...)` for callers that only see the
  built Application or a factory.
- `mount(..., middleware=[mw1, mw2])` chains FastMCP `Middleware`
  instances onto the mounted server after Theodosia's built-in
  input-coercion middleware. Useful for OTel spans on every MCP call,
  rate limiting, structured logging.
- `mount_multi(...)` now accepts both kwargs too and forwards them to
  each sub-application's `mount()` call (previously a `TypeError`).
- Tool annotations: the four MCP tools carry `ToolAnnotations(
  destructiveHint, idempotentHint, openWorldHint)` so capable clients
  render the right confirmations.
- 13 tests total across `tests/test_mount_hooks.py`,
  `tests/test_mount_middleware.py`, `tests/test_mount_multi.py`,
  `tests/test_tool_annotations.py`, `tests/test_streaming_progress.py`.

### Added (`theodosia.drive_claude`)

One-line glue between a mounted server and the Anthropic SDK. Lists the
FSM's tools, injects `theodosia://graph` / `state` / `next` into the
system prompt, loops turn-by-turn until terminal or `max_turns`. Optional
`[claude]` extra (anthropic>=0.40). Re-exported from the top level.

### Added (`theodosia sessions diff <a> <b>`)

Cross-session post-mortem comparison. Useful when you want to ask
"how did this session diverge from a known-good one?".

- Two app ids (full uuid or prefix).
- Output: where the action paths diverged (common prefix + each
  session's continuation), and a key-by-key state diff at the final
  step (added / removed / changed values).
- ``--json`` for scripted consumption.
- Pure tracker-log reader. No live mounted server needed.

### Changed (CLI package split for maintainability)

``cli.py`` (1,884 lines) is now ``cli/`` (9 modules, largest 348 lines).
Same public surface; no behavioral change. One module per command family.

- ``cli/__init__.py``: public re-exports of every name tests and
  ``theodosia.__init__`` reach for. ``from theodosia.cli import build_cli,
  run, app, _BRANDING, _read_steps, _resolve_app, _burr_ui_url`` etc all
  unchanged.
- ``cli/_branding.py``: Rose Pine theme, ``Console`` instances, the
  ``_Branding`` dataclass and singleton.
- ``cli/_resolve.py``: target imports, tracker home resolution, app/project
  lookup, Burr UI URL builder.
- ``cli/_steps.py``: ``StepRow`` model, tracker log + refusal log readers,
  state-diff text, table builder, scan helpers.
- ``cli/_topology.py``: ``_Topology`` model, graph rendering, ``render``
  command.
- ``cli/sessions.py``: ``sessions ls / show / tail``, ``watch``, ``logs``.
- ``cli/reports.py``: ``report`` command + markdown rendering + webhook POST.
- ``cli/status.py``: ``status`` and ``verify`` commands.
- ``cli/_app.py``: ``serve``, ``doctor``, ``ui``, ``build_cli``, ``run``.

The ``_BRANDING`` singleton is now mutated in place by ``_set_branding``
so cross-module ``from … import _BRANDING`` references stay live; the
single-file ``global _BRANDING; _BRANDING = …`` pattern only worked when
all consumers lived in one module.

_The kwargs and deep-link entries that lived here have been folded
into the consolidated entries near the top of this Unreleased section._

### Fixed (round 18: hard timeout boundary; storage coupling; radon hygiene)
- **`action_timeout_seconds` now fires at the wall-clock budget regardless
  of whether the inner await honors cancellation.** Previously
  ``asyncio.wait_for`` waited for the cancelled task to acknowledge
  ``CancelledError``; an action body awaiting on a ``ctx.sample`` or
  ``ctx.elicit`` server-to-client request sat for the full FastMCP request
  timeout (~30s) because FastMCP's elicit/sample do not propagate
  cancellation cleanly. The new ``_race_with_timeout`` helper uses
  ``asyncio.wait`` so the timer fires at the boundary; the orphaned task
  continues until its own internals unwind. Documented in ``refusals.md``
  with the in-memory transport caveat (FastMCP serializes outgoing
  responses behind outstanding elicit requests on in-memory; production
  http / stdio / sse transports get the wire response at the budget).
- **`theodosia.tracker()` honors ``THEODOSIA_HOME`` env and the
  ``build_cli(home=...)`` value.** Resolution order: explicit ``storage_dir``
  arg → ``THEODOSIA_HOME`` env → ``_BRANDING.home`` for the current
  process → ``~/.theodosia``. A downstream rebrand calling
  ``build_cli(home="~/.my-fsm")`` no longer has to thread ``storage_dir``
  through every ``theodosia.tracker(project=...)`` call to keep the CLI's
  reads and the tracker's writes pointed at the same root.
- **`_resolve_app` extracted into ``_pick_default_project``,
  ``_pick_default_app_id``, ``_resolve_app_id_prefix``, and ``_bail`` helpers.**
  Missing home / project / app id now exit 1 with a clean rich-rendered
  message instead of a raw ``FileNotFoundError`` traceback. ``_resolve_app``
  dropped off the C-rank radon list.
- **`verify_ledger` extracted ``_check_entry`` per-entry helper.** Main
  loop reads as a one-liner per entry; radon dropped off the C list.

### Fixed (round 17: composition-surface audits with scope discipline)

Three parallel sim agents on under-tested Theodosia surfaces, briefed to
label findings `[theodosia]` / `[burr]` / `[fastmcp]` / `[typer]` /
`[mcp-spec]` / `[unclear]`. Only `[theodosia]`-labeled findings became
grind targets; the rest get routed upstream or documented honestly.

**Sim M (`spawn_subapp` + `mount_multi`):**
- **`theodosia://subruns/{id}.history` now populates per-action** for every
  sub-run, not just when the sub-Application wires its own
  `LocalTrackingClient`. `spawn_subapp` drives the sub-app via `astep` in
  a loop, recording one history entry (with `seq`, `action`, and
  post-step `state`) per action it ran. Old behavior depended on Burr's
  tracker JSONL existing in a path Theodosia could find. The new
  behavior works for any sub-Application.
- **Recursive `spawn_subapp` records `parent_subrun_id`**, so a nested
  spawn tree can be reconstructed from `theodosia://subruns`. Previously
  every sub-run reported the outermost session as parent; you could not
  tell which spawn nested inside which.
- `mount_multi` namespacing, isolation, refusal-scoping, and 5-graph
  scaling all held without changes.

**Sim O (`build_cli` downstream rebrand):**
- **`--version` honors `prog_name`.** `my-fsm --version` reports `my-fsm
  <version>` instead of `theodosia 0.2.0`, falling back to Theodosia's
  version when the downstream package's metadata is missing.
- **`status` banner uses `prog_name`.** The launch-banner header now
  reads `<prog_name> <version>` instead of hard-coded `theodosia`.
- **`primer` no longer registers under rebranded CLIs.** The primer
  command is a Theodosia-specific demo whose panel, footer, and URLs
  name the Theodosia project explicitly. A downstream `my-fsm` would
  otherwise advertise theodosia in its first 30-second user experience.
- **All nine `--home` `--help` strings** dropped the hardcoded
  `~/.theodosia` mention. They now say "Tracker storage root. Overrides
  the CLI default (see --help)." which is accurate for both branded and
  rebranded CLIs.
- **`build_cli` and `run_cli`** are now re-exported from `theodosia`
  top-level so downstream packages do not need to import from
  `theodosia.cli`.

**Sim N (`ctx.sample` / `ctx.elicit` / `current_mcp_context`):**
- `ctx.sample` integration works against capable clients;
  no-handler refusal cleanly surfaces as `action_error`.
- `ctx.elicit` accept/decline/timeout paths work for capable clients.
- `current_mcp_context()` is session-isolated under concurrent overlapping
  sampling roundtrips (contextvars-backed).
- **Known limit**: `action_timeout_seconds` does NOT cancel an in-flight
  `ctx.sample` / `ctx.elicit` roundtrip. The async-cancel scope does not
  extend over server-to-client requests. Documented; the fix lives in a
  later round.

### Fixed (round 16: adversarial / break-the-rails audits)

Three parallel adversarial sim agents whose explicit job was to break
Theodosia. Findings collated; honest doc corrections + the fixes that
ship cleanly in this round.

- **Ledger entries now carry the session's `app_id`, `project`, and
  `partition_key` in the hashed payload.** Copying `ledger.jsonl` between
  session directories was previously undetected by `theodosia verify`;
  Sim L's cross-session-replay forgery (#12 in their attack table) now
  fails verification because the binding does not match the on-disk path.
- **Optional HMAC mode for the ledger.** Setting `THEODOSIA_LEDGER_KEY`
  (hex-encoded bytes) in the server environment switches the chain from
  plain SHA256 to HMAC-SHA256. Default unkeyed mode is fast and detects
  in-place edits; HMAC mode raises forgery work-factor from "Python
  one-liner" to "extract the key." Both modes are documented in
  `ledger.py` and the new `security-model.md` section.
- **`security-model.md` rewritten** to document what the ledger DOES and
  DOES NOT prove (truncation, whole-cloth forgery in unkeyed mode,
  origin, and existence of a session are all out of scope) and what
  `reads=` DOES and DOES NOT enforce (action-body discipline yes;
  wire-level confidentiality no, because step responses and
  `theodosia://state` return full state). Plus the no-state-cap and
  no-fork-GC realities Sim K's stress run surfaced.
- **`authoring.md` honest correction**: `reads=` is enforced for the
  action body via Pydantic projection but is not a confidentiality
  boundary on the MCP wire. Earlier framing implied otherwise.
- **`classify_payload` detects nested error envelopes** up to 3 levels
  deep. Previously, upstreams returning `{"data": {"error": "..."}}`
  silently classified as OK; they now classify as ERROR with the nested
  message in `detail`.
- **Persona files with malformed YAML frontmatter** previously crashed
  `mount()` at startup with `yaml.scanner.ScannerError`. Now caught and
  re-raised as a `ValueError` that names the file and the YAML error;
  the rest of the persona directory still loads.

### Documented limits (not fixes, but honesty)
- State size and depth are not capped by Theodosia. A 100MB state value
  or a 500-level-nested dict will be materialized in full in the step
  response and the tracker log. Cap in the action body.
- `max_sessions` caps the in-memory FastMCP session map, not on-disk
  fork directories. `fork_at` / `fork_from_past` each create a fresh
  `app_id` directory; reap out of band.
- Ledger truncation (dropping the tail-most entry) is still undetected
  without external commitment. The fix is structural (Merkle root posted
  to a transparency log or append-only object storage); planned for a
  later release.

### Fixed (round 15: remaining-prompt-pattern parallel audits)

Three more parallel sim-agent audits, building the remaining classic
prompt patterns as FSMs. All three returned ship-it verdicts.

- **Sim G (ReWOO, plan-first-no-observation)**: confirmed the
  no-inline-observation property is structurally enforced by the FSM
  topology, not just a prompt instruction. 3-5x token savings matched
  the paper. Verdict: ship.
- **Sim H (Chain-of-Verification)**: discovered that Burr's
  `reads=[...]` declaration enforces per-action input independence via
  the synthesized Pydantic input model. The verifier action literally
  cannot access the baseline because the field doesn't exist on its
  projected input (`AttributeError`, not `None`). This is the
  structural enforcement that makes CoVe's independence property
  defensible. New section in `authoring.md` documenting it.
- **Sim I (Plan-and-Execute)**: `reads=["last_eval", ...]` forces the
  planner to engage with the failure signal: the action body cannot
  run without that field in scope. Termination cap is a transition
  edge, not a counter. Verdict: ship over LangChain's Plan-and-Execute.

### Documented
- `SourceResult.status` is the lowercase string `"ok"` / `"error"` /
  `"malformed"`. The uppercase `OK` / `ERROR` / `MALFORMED` constants
  are the public identifiers; compare to those, not bare uppercase
  strings.
- `FakeUpstream.register` accepts static values, sync callables, or
  async callables taking the args dict. Spelled out in the docstring.

### Fixed (round 14: prompt-pattern parallel audits)

Three more parallel sim-agent audits, this round building classic prompt
patterns as Theodosia FSMs: Reflexion / Self-Refine, Tree-of-Thoughts
(Game of 24), and Deep Research with classified upstream chaos. All three
named the same two CLI papercuts; both fixed.

- **`theodosia sessions ls` hides ghost-empty tracker entries by default.**
  FastMCP creates a tracker directory per `Client` connect even when no
  step lands; previous behavior listed every one as `(empty)` and
  drowned the real sessions. New `--all` flag restores the old behavior.
- **`theodosia sessions ls -p <project>` auto-falls back to `~/.burr`.**
  A project wired through Burr's own `LocalTrackingClient(project=...)`
  writes there, not to `~/.theodosia`. The CLI now checks both roots when
  a project name is given and emits a one-line hint when it auto-switches
  so users learn to pass `--home ~/.burr` next time.
- **`theodosia doctor` warns on sync action bodies under a persister or
  tracker.** Burr's `post_run_step` fires with pre-step state for sync
  bodies; the on-disk tracker rows record stale state for those rows and
  `fork_from_past` resumes from the wrong snapshot. Authors writing
  `async def` avoid the trap; the new check surfaces it at validation
  time instead of after a confused report run.
- **`theodosia sessions list`** is now a hidden alias for `sessions ls`
  (muscle-memory fallback); the previous behavior wrapped a Typer
  command-not-found error in a giant Rich-rendered traceback panel.
  `pretty_exceptions_enable=False` on the root CLI also keeps unexpected
  errors as plain text instead of multi-page panels.

### Fixed (round 13: three parallel sim-agent audits)

Three doc-only audits ran in parallel (SRE incident triage, content
moderation, PR review with real stdio MCP upstream over HTTP). All three
named the typed-input gap as their #1 blocker. Other findings collated.

- **Pydantic typed-input coercion now handles `Optional[Model]` and
  `Model | None`**. Previously the coercion only fired when the annotation
  was a bare `BaseModel` subclass; `Optional[OrderInput]` silently skipped
  and the action body received a dict, crashing on `dict.model_dump()`.
  The check now unwraps `Optional` / `Union[X, None]` (and bare `T | None`)
  via `typing.get_origin/args`.
- **Pydantic shape errors now surface as `validation_failed`, not
  `action_error`.** Previously the coercion caught `ValidationError` and
  silently passed the raw dict through to the action body, which then
  crashed with an opaque `AttributeError`. The handler now raises
  `ValidationFailed` carrying per-field Pydantic errors so the wire
  response is a clean `validation_failed` refusal the LLM can recover from.
  Regression test in `tests/test_pydantic_validation_failed.py`.
- **`Assembly.to_yaml()` round-trips callable workflows.** Previously a
  factory callable raised `yaml.RepresenterError` because PyYAML cannot
  represent a function. `to_yaml` now resolves a callable workflow to its
  `module:attr` dotted-path automatically, or raises a clear `ValueError`
  if the function has no resolvable import (e.g. a closure or
  ``__main__``). A built `Application` still cannot round-trip; the new
  error message says so directly.
- **`call_upstream` unwraps single-key `{"result": ...}` envelopes.**
  FastMCP wraps scalar tool returns this way; action bodies previously
  had to unwrap manually, contradicting `upstream.md`'s "calls return the
  tool's structured result" promise. Dict / list returns pass through
  unchanged.
- **`theodosia report` flags a possibly-stale terminal state.** When the
  terminal action body is sync and Burr's `post_run_step` records pre-step
  state in the tracker, the report now prefaces the "Final state" block
  with a note explaining the snapshot is one step behind and pointing at
  `theodosia://state` / `async def` as recovery paths. Detected via
  `__PRIOR_STEP` mismatch on the terminal row.
- **`authoring.md` documents Trap 3**: `Condition.expr` evaluates against
  pre-step state, so a field written in action N gates the N → N+1 edge,
  not the N-1 → N edge.

### Fixed (round 12: sync action timeout actually preempts)
- **`action_timeout_seconds` now preempts sync action bodies.** Previously
  a sync body (`time.sleep`, blocking HTTP, tight CPU loop) blocked the
  event loop, which defeated `asyncio.wait_for`: the cancellation timer
  could not tick while the loop was blocked. Theodosia now detects sync
  bodies and runs them via `asyncio.to_thread` so blocking happens off
  the main loop and the timer fires regardless. The orphaned thread keeps
  running (Python cannot safely kill threads), but the client gets the
  structured `action_timeout` refusal at the budget boundary. Async
  bodies stay on the main loop where ctx-injection works. Caught by the
  round-12 evaluation audit; regression test in
  `tests/test_sync_action_timeout.py`. `refusals.md` documents the
  coverage and the orphaned-thread caveat.

### Fixed (round 10: 0.4.0-blocker doc fixes)
- **`THEODOSIA_VERBOSE` documented in `cli.md`** with what it restores
  (Burr's error panel + traceback, FastMCP per-call DEBUG) and when to
  use it. Previously the env var was undocumented anywhere on the site.
- **`theodosia status` empty-state hint.** When no projects exist under
  `~/.theodosia` but `~/.burr` does, the empty message now suggests
  `theodosia status --home ~/.burr`. A user who wired Burr's native
  tracker no longer sees an empty table with no path forward.

### Fixed (round 9: static-analysis sweep + table widths)
- **Responsive table widths in `theodosia status` and `theodosia sessions ls`.**
  Both tables now show a 12-char `app_id` prefix, an 18-char `last action`
  cell, and a relative `when` column (`3m ago`, `2h ago`, `4d ago`) instead
  of full ISO timestamps. Previously columns truncated mid-word and the
  trailing border was garbled on standard terminals. Cosmetic but the most
  visible CLI surface for daily use, so worth tightening.
- **Static analysis sweep**: clean across ruff, refurb (3 hits fixed:
  `dict()` → `.copy()`, redundant `is not True`, comprehension), mypy
  (21 errors → 0; persona `out` redefined, `_build_persona_frame`
  None-guard on `entry.application`, transport literal narrowed in
  `theodosia serve`), and vulture (no live-code dead code). Radon
  flags `status`, `sessions_ls`, and `_graph_renderable` as D-rank
  (long but single-purpose render code); deferred to a 0.4.1 refactor.

### Fixed (round 8: seventh exploration audit)
- **Persona placeholders render dicts and lists as JSON, not Python
  `repr`.** `{state.order}` for `state.order = {"item": "soda", "qty": 1}`
  now renders `{"item": "soda", "qty": 1}` (double quotes) so an LLM
  consuming the rendered persona prompt sees JSON, not single-quoted
  Python. Scalars still go through `str()`. Documented in `personas.md`.
- **`observability.md` terminal-CLI inventory** now includes `theodosia
  status`, `theodosia report`, and `theodosia verify`. Previously asymmetric
  with `cli.md`.

### Fixed (round 7: sixth exploration audit)
- **`theodosia status` "empty" status actually fires.** The previous round
  added the "empty" label to `sessions ls` but missed the second code path
  in `status`, which kept reporting `running` for 0-step sessions. Both
  paths now emit `empty` consistently, and the rich table maps `empty` to
  a muted style.
- **`tutorial.md` drive_rover.py** still passed a built `Application` to
  `mount()` on one line (line 369) after the prior round caught two of
  three. Fixed; tutorial.md now uses the factory form throughout, matching
  `authoring.md`.
- **`cli.md` observability section** now lists `theodosia status` and
  `theodosia report` alongside `sessions`, `logs`, and `verify`, with a
  short note on the `last_status` values (`ok` / `error` / `empty` /
  `running`).

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
