# CLAUDE.md

Project context for Claude Code working in this repo.

## What this is

**BurrMCP** is an adapter library that mounts [Burr](https://burr.dagworks.io/)
state-machine `Application` instances as [FastMCP](https://gofastmcp.com/)
servers. Each Burr `@action` is exposed to MCP, state lives server-side,
and connecting MCP clients can only call actions reachable from the
current state. Refusals carry `valid_next_actions` so the agent can
self-correct from a single error.

Pitch: **FSM-as-API, not tools-as-API**. The architectural property
that holds across 19 demos: in `STEP` mode, every server presents
exactly four MCP tools (`step`, `reset_session`, `fork_at`,
`fork_from_past`) regardless of how complex the FSM is. The action
namespace lives in the `step` tool's `action` argument schema,
discoverable via `burr://graph`.

Brand: **BurrMCP**. Package: `burrmcp` (no separator). Python import:
`import burrmcp`. CLI: `burrmcp serve module:attr` and
`burrmcp doctor module:attr`. Mirrors the FastMCP/fastmcp convention.

## Repo layout

```
src/burrmcp/
  adapter.py        mount() + ServingMode + per-session store + meta tools
  cli.py            Typer-driven `burrmcp serve` / `burrmcp doctor` entry
  doctor.py         Static validation against a Burr Application
  importing.py      burr_app_from_fastmcp + ToolSpec for lifting flat servers
  __init__.py       Public surface: mount, ServingMode, ToolSpec, spawn_subapp
examples/           19 demo FSMs (see "Demo lineup")
  data/             Shipped sample data for parallel_research and codebase_security
  skills/           5 real Claude Code SKILLs pulled verbatim with attribution
  claude-code.example.json   The canonical .mcp.json users paste into ~/.claude.json
  mcphost.example.json       The mcphost-flavored equivalent
tests/              333 tests; pytest tests
```

## Demo lineup (19 total)

Grouped by what they exercise:

**Pure FSM, no external deps:**
- `coffee_order`: linear (the canonical "smallest interesting FSM")
- `triage`: classify-then-route
- `adventure`: branching state traversal
- `chargen`: sequential narrowing wizard with strict ordering
- `release_pipeline`: agent refuses to skip steps; gated tests/canary/promote
- `local_shell`: read-before-edit safety rails; patch-overlay via state
- `incident_response`: workflow with `spawn_subapp` investigation sub-graph
- `subgraphs`: sub-Application composition demo
- `ml_training`: non-LLM iterative training (pure stdlib logistic regression)
- `streaming_narrate`: streaming actions as MCP progress notifications
- `skill_security_audit`: caller-LLM-driven SKILL-to-FSM, no server-side LLM

**Shellout / deterministic tooling:**
- `unix_health`: real `df`/`uptime`/`ps`/`vm_stat` / `free` shellouts
- `codebase_security`: real `bandit` + `detect-secrets` against shipped
  `vuln_demo` repo; patch-overlay loop never edits the source
- `git_review`: wraps `git status`/`log`/`show` via subprocess

**LLM-in-the-graph (server-side calls):**
- `granite_oncall`: Granite via Ollama; retry-as-transitions for malformed
  classifier output (max 3) before `route_to_human`
- `adaptive_crag`: Granite self-grading RAG; query rewrite loop on bad
  grade. Simplified CRAG (Yan et al 2024).
- `mellea_qiskit_migration`: Mellea's session.instruct wrapped as one
  Burr action; FSM owns workflow; Mellea owns the IVR loop

**RAG / parallel:**
- `parallel_research`: `asyncio.gather` fan-out per source folder over the
  shipped markdown corpus; accepts `corpus_dir` override

**Observability:**
- `with_otel`: `OpenTelemetryBridge` lifecycle adapter wired into the
  factory; spans flow for every action

## Conventions

- **No em-dashes.** Anywhere in authored prose (`—`, `–`). Hyphens, commas,
  semicolons, parens are fine. Third-party content (e.g., the SKILLs under
  `examples/skills/`) is kept verbatim and untouched.
- **No marketing language.** No "blazing fast", "powerful", "robust",
  "comprehensive", "production-grade". State what something does.
- **No Claude/AI attribution in commits, comments, or code.** Commits stay
  clean of `Co-Authored-By: Claude` lines per user's standing instruction.
- **Tracker projects.** Every narrative demo wires a
  `LocalTrackingClient(project="<demo>-demo")` so `burr://trace` returns real
  data. Conftest's autouse `_isolate_burr_home` redirects HOME so tests
  don't leak into `~/.burr/`.
- **Hermetic tests.** LLM-calling and shellout-driven demos all have a
  monkey-patchable indirection (`_call_granite`, `_call_mellea`,
  `_run_check_command`) so tests run without Ollama or any subprocess. Real
  scanner tests (`test_codebase_security`) DO run real `bandit` and
  `detect-secrets` because those install via uv sync.
- **Action validation goes inside the action body.** Transition gates
  refuse with `invalid_transition`; action-level checks (e.g.,
  "must read file before editing") raise `ValueError` and surface as
  `action_error` with the full message. Both forms work for refusing
  agent moves.

## Dev setup

```bash
cd ~/burr-mcp
uv sync                            # pulls burr, fastmcp, typer, pydantic,
                                   # plus dev deps (bandit, detect-secrets, psutil)
uv run pytest                      # ~333 tests, ~16s
uv run ruff check src/burrmcp examples tests
uv run ruff format src/burrmcp examples tests
uv run burrmcp doctor coffee_order:build_application --app-dir examples
```

The user's testing dir for the demo MCP servers is at
`~/burr-mcp-demo/.mcp.json`. It wires 14 zero-setup demos (everything
except those needing Ollama, Mellea, OTel, or a git context).

## Status

- **Version:** 1.12.0
- **Tests:** 333 passing in ~16s
- **Demos:** 19 in `examples/`
- **Skills shipped:** 5 in `examples/skills/`, one converted
  (`skill_security_audit.py`)
- **GitHub:** `git@github.com:msradam/burr-mcp.git` (private)
- **PyPI:** not published yet, deliberately

Recent commits worth knowing:
- `83c3331` drop examples/import_flat.py (library feature kept; example
  used an async factory that didn't fit `burrmcp serve` shape)
- `572882c` mellea_qiskit_migration demo
- `13f603d` skill_security_audit + 5 real SKILLs in `examples/skills/`
- `0e9ce2e` v1.12.0 rename `burr_mcp` -> `burrmcp`, brand `BurrMCP`
- `f3d1480` unix_health shellout rewrite; research demos accept
  `corpus_dir`
- `c143463` v1.11.0 unix_health + codebase_security + adaptive_crag
- `6041978` Typer CLI

## Open threads

In rough order of leverage:

1. **Validate Burr ecosystem mounts unchanged.** The pitch "any Burr graph
   is also an MCP server" needs proof. Pull 3-4 of Burr's own examples
   (Hamilton integration, custom persister, MapStates, burr-ray
   distributed) and mount each via `burrmcp serve` with zero
   modifications. Each one that works ships as an `upstream_<name>.py`
   demo with a docstring noting it's literally upstream code. Each one
   that fails is a gap to close in the adapter, not the demo.

2. **Compatibility matrix.** Once (1) lands, write a "Burr feature ->
   BurrMCP support" table in the README. Every feature: "works through
   `mount()` unchanged" or "needs <X> to mount". Source-of-truth for
   the "any Burr graph just works" claim.

3. **More SKILL-to-FSM conversions.** Four SKILLs sitting in
   `examples/skills/` as reference: `claude-api`, `mcp-builder`,
   `webapp-testing`, `skill-creator`. `mcp-builder` is hilariously
   recursive (a SKILL about building MCP servers, decomposed into an
   FSM mounted as an MCP server). `webapp-testing` pairs Playwright
   patterns with FSM gates.

4. **More Mellea samples.** SOFAI graph coloring (two-tier model
   escalation) and Granite Guardian function-call repair were the
   runner-up picks from the survey. Both are <100 lines upstream.

5. **Auto-describe in instructions.** `mount()` could append a synthesized
   action listing to the user-passed `instructions` string so the
   caller LLM sees the action surface at MCP connect-time rather than
   having to read `burr://graph` first. ~50 lines.

6. **fork_at + fork_from_past internal dedup.** The two meta-tools share
   most of their body (rebuild via factory, inject state + `__PRIOR_STEP`,
   clear subruns). Extract a `_restore_snapshot()` helper; both call it.
   No public surface change.

7. **DYNAMIC mode refusal payload.** When a per-session-disabled tool is
   called in DYNAMIC mode, FastMCP returns a generic "tool not enabled"
   error. STEP returns a rich `invalid_transition` payload. Aligning
   DYNAMIC to return the same shape would make it useful in
   stale-client contexts. ~30 lines.

8. **Multi-Application mount.** One MCP server hosting multiple
   Applications side-by-side; agent picks which to drive via a
   meta-tool. Architectural; ~100 lines in `mount()`.

9. **README navigation pass.** README has grown enough that a "start
   here" + table-of-contents section at the top would help drop-in
   readers.

10. **PyPI publish.** Deferred deliberately. Pre-PyPI window lets us
    keep iterating on package surface (e.g. the `burr_mcp -> burrmcp`
    rename was free this way).

## Things that won't change

- The four-MCP-tool surface in STEP mode. That's the architectural
  property.
- The Burr `Application` boundary as the integration point. Anything
  Burr supports through `ApplicationBuilder` is fair game and should
  pass through `mount()` transparently.
- `LocalTrackingClient` on every narrative demo, so `burr://trace` and
  the Burr UI replay always show real data.
- The voice constraints (no em-dashes, no marketing, no Claude
  attribution).
