# BurrMCP

**FSM-as-API, not tools-as-API.**

Mount a [Burr](https://burr.dagworks.io/) state machine as an [MCP](https://modelcontextprotocol.io/) server. The agent gets four tools (`step`, `reset_session`, `fork_at`, `fork_from_past`) regardless of how complex the FSM is. State lives on the server, transitions are enforced, and refusals carry the actions that *are* reachable so the agent can self-correct from a single error.

```python
from burrmcp import mount

server = mount(application)
server.run()
```

The action namespace lives in the `step` tool's argument schema, discoverable via the `burr://graph` resource. Out-of-order calls come back as structured `invalid_transition` errors listing valid next actions. See [Coffee in 30 lines](#coffee-in-30-lines) for a runnable example.

You can also go the other direction: lift an existing flat FastMCP server into a Burr Application with `burr_app_from_fastmcp(...)`, gaining transition enforcement and per-session isolation without rewriting your tools. See [Lifting an existing FastMCP server](#lifting-an-existing-fastmcp-server).

Status: v1.12.0.

## What this is

An adapter library. You write a Burr state machine. `mount(application)`
returns a FastMCP server. That's the whole pitch.

The adapter does the wiring:

- Each Burr `@action` becomes an MCP tool (or one meta-tool, or a
  state-gated tool list; see "serving modes" below).
- The full graph is advertised as a static `burr://graph` resource so
  a connecting agent learns the topology in one read.
- Current state and valid-next-actions are inlined on every tool
  response so the agent doesn't have to keep polling.
- Per-session isolation via factory mode; TTL/LRU eviction; session
  locks so concurrent calls don't race on Burr's not-thread-safe
  Application instance.
- Sub-Application composition: an action can call
  `spawn_subapp(sub_app)` and the nested timeline becomes addressable
  at `burr://subruns/{id}`.
- Five structured refusal classes (invalid transition, unknown action,
  action error, action timeout, validation failed) recorded in a
  per-session history resource.

`mount(...)` accepts either a built `Application` (shared across all
connected clients) or a callable `() -> Application` (each MCP
session gets its own state). The factory form is the right default
for multi-client servers; the instance form is fine for single-user
local tooling.

The example library covers the common shapes: see [Examples](#examples)
for a CLI-wrapping git review server, a text adventure, an incident-
response showcase, a flat-server lift, a sub-graph composition, and
the toy coffee/triage FSMs. These mirror patterns from
[Burr's own example library](https://github.com/apache/burr/tree/main/examples)
applied to the MCP wire format.

## Why bother

Other "FSM as MCP server" projects exist (LangGraph's MCP endpoint,
Step Functions Tool MCP Server, the Temporal MCP servers). They all
expose the whole machine as one opaque tool with arguments. BurrMCP
exposes each node of the graph as its own tool, and lets the server
decide which calls are valid given the current state. That difference
matters when the client is an LLM picking from a tool menu: the menu
is the graph, not a single black-box call.

## What works through `mount()`

The integration boundary is Burr's `Application`. Anything supported by
`ApplicationBuilder` passes through `mount()` without adapter changes,
including parallelism, persistence, telemetry, and library coexistence:

| Burr surface | Through `mount()` | Demo / evidence |
|---|---|---|
| `@action`, `with_transitions`, `with_state`, `with_entrypoint` | Yes (core path) | every demo |
| `Condition.expr` / `.when` / `.default` | Yes | `release_pipeline`, `chargen` |
| `with_tracker(LocalTrackingClient)` | Yes; surfaced at `burr://trace` | every narrative demo |
| `with_state_persister(BaseStatePersister)` | Yes | `sqlite_persister` |
| `with_typed_state(Pydantic)` | Yes | `tests/test_typed_state.py` |
| `with_parallel_executor(...)` | Yes (default thread-pool); `RayExecutor` swap documented inline | `burr_map_parallel` |
| `MapStates` / parallel sub-runs | Yes | `burr_map_parallel` |
| Streaming actions | Yes; emitted as MCP progress notifications | `streaming_narrate` |
| Async actions (`async def @action`) | Yes | `parallel_research`, `mellea_qiskit_migration` |
| Sub-Application composition | Yes; `burr://subruns` indexes `spawn_subapp` calls | `incident_response`, `subgraphs` |
| OpenTelemetry (`OpenTelemetryBridge`) | Yes | `with_otel` |
| Hamilton driver inside an action body | Yes (no special integration) | `hamilton_features` |
| `app.run(halt_after=...)` auto-routing | Burr-level only | MCP path always uses agent-chosen actions via `step` |

Anything missing from this table either hasn't been exercised yet or
genuinely needs adapter work; both cases are tracked in the
project-internal feature roadmap.

## Three serving modes

```python
from burrmcp import mount, ServingMode

server = mount(application, mode=ServingMode.STEP)  # default
```

| Mode | Tools exposed | Transition enforcement | Client compatibility |
|---|---|---|---|
| `TOOLS` | One per `@action` | None (graph is advisory) | Universal |
| `STEP` | One meta-tool: `step(action, inputs)` | Yes, server-side | Universal |
| `DYNAMIC` | One per `@action`, visibility tracks state | Yes, server-side | Needs `tools/list_changed` support |

Default is `STEP` because dynamic tool lists are not handled the same
way across MCP clients. Claude Code [ignores `tools/list_changed`](https://github.com/anthropics/claude-code/issues/13646),
Cursor [doesn't refresh on its own](https://forum.cursor.com/t/mcps-tool-list-changed-not-picked-up/132363),
Claude Desktop's behavior is inconsistent across versions. `STEP` keeps
the tool list static; the server enforces transitions inside the meta
tool.

## Lifting an existing FastMCP server

The most common starting point isn't "I have a Burr graph and I want
to serve it" but "I have a FastMCP server with a bunch of tools and
I want to add transition enforcement and an audit trail." For that,
the importer:

```python
from fastmcp import FastMCP
from burrmcp import ServingMode, ToolSpec, burr_app_from_fastmcp, mount

flat = FastMCP("legacy")

@flat.tool
async def create_order(item: str) -> dict:
    return {"order_id": "ORD-1", "item": item}

@flat.tool
async def pay(order_id: str, amount: float) -> dict:
    return {"paid": True, "receipt": "R-99"}

@flat.tool
async def fulfill(order_id: str) -> dict:
    return {"status": "fulfilled"}

# Declare the implicit state machine.
app = await burr_app_from_fastmcp(
    flat,
    entrypoint="create_order",
    initial_state={"order_id": None, "paid": False},
    tool_specs={
        "create_order": ToolSpec(writes=["order_id"], merge_result=True),
        "pay":          ToolSpec(reads=["order_id"], writes=["paid"], merge_result=True),
        "fulfill":      ToolSpec(reads=["order_id", "paid"]),
    },
    transitions=[("create_order", "pay"), ("pay", "fulfill")],
)

server = mount(app, mode=ServingMode.STEP, name="lifted")
```

The verbosity sits on `tool_specs` and `transitions`. That's the user
articulating the state machine their tools were already describing.
The library doesn't try to guess: declaring it explicitly is the only
honest move because parameter names don't tell you which tools mutate
shared state. What carries over from the original tools without any
declaration: parameter names, types, defaults, docstrings, async/sync
nature. ``tests/test_importing.py`` exercises every supported
``ToolSpec`` knob end-to-end.

`ToolSpec` also accepts `state_update` (an explicit callable taking
the tool's result and returning state mutations, overrides
`merge_result`) and `rename` (change the action's name in the Burr
graph, useful when merging multiple flat servers).

## Coffee in 30 lines

```python
from burr.core import action, ApplicationBuilder, State
from burrmcp import mount, ServingMode

@action(reads=[], writes=["stage", "item", "qty"])
def take_order(state: State, item: str, qty: int = 1) -> State:
    """Place a new coffee order."""
    return state.update(stage="ordered", item=item, qty=qty)

@action(reads=["stage"], writes=["stage", "paid_amount"])
def pay(state: State, amount: float) -> State:
    """Pay for the placed order."""
    return state.update(stage="paid", paid_amount=amount)

@action(reads=["stage"], writes=["stage"])
def fulfill(state: State) -> State:
    """Mark the order fulfilled. Terminal."""
    return state.update(stage="fulfilled")

app = (
    ApplicationBuilder()
    .with_actions(take_order=take_order, pay=pay, fulfill=fulfill)
    .with_transitions(("take_order", "pay"), ("pay", "fulfill"))
    .with_state(stage="new")
    .with_entrypoint("take_order")
    .build()
)

mount(app, mode=ServingMode.STEP, name="coffee").run()
```

A client that calls `pay` before `take_order` gets:

```json
{
  "error": "invalid_transition",
  "requested": "pay",
  "valid_next_actions": ["take_order"],
  "message": "action 'pay' is not reachable from current state. Valid actions now: ['take_order']."
}
```

The list of valid actions is in the response, so a client that doesn't
keep its own model of the graph can recover from one error.

## Resources

Every mounted server registers eight MCP resources:

| URI | Returns |
|---|---|
| `burr://graph` | Static description of the FSM topology: every action with its description, reads, writes, required/optional inputs; every transition with its condition. Computed once at mount time. **Read this first** to see the whole graph in one fetch. |
| `burr://state` | Current Application state as JSON. Internal Burr keys (`__PRIOR_STEP`, `__SEQUENCE_ID`) filtered. |
| `burr://next` | Action names reachable from the current state. Empty list after a terminal action. |
| `burr://history` | Per-session timeline of every action attempted (successes and refusals). |
| `burr://trace` | Burr's on-disk `LocalTrackingClient` log for the current session's Application. Capped at 1000 most-recent records. Returns `{"error": "no_tracker"}` if no `LocalTrackingClient` was attached. |
| `burr://session` | Tracker coordinates for this session: `{project, app_id, app_dir, partition_key}`. Lets a client (or terminal tooling) locate the session's data on disk without guessing. `project` and `app_dir` are null when no `LocalTrackingClient` is attached. |
| `burr://subruns` | Index of sub-Application runs spawned in this session via `spawn_subapp`. Each entry has `id`, `uri`, `label`, `started_ts`, `ended_ts`, `parent_action`. |
| `burr://subruns/{id}` | Full record for one sub-run: id, label, timestamps, in-memory history, final state, and any error. |

**Discovery flow for a connecting client**: read `burr://graph` once
at start to learn the topology, then drive on step responses
(`state`, `valid_next_actions` are inline on every response).
Re-read `burr://state` only for forensic checks; `burr://next` only
when you need a refresher mid-session. The server-level instructions
include a one-line hint pointing at this flow, so the model sees it
before the first tool call.

**Reset.** Every mounted server registers a `reset_session` MCP tool
that's always callable regardless of FSM state. In factory mode it
rebuilds the session's Application from the factory, clears
sub-runs, and appends a `reset_session` marker to history (prior
entries preserved). Useful when an agent reaches a terminal state or
a dead-end branch and wants to try a different path without
disconnecting. Refuses in shared-app mode where the operation would
affect every connected client.

`burr://history` and `burr://trace` are complementary. History is one
entry per attempted action (including refusals), structured for the
client to act on. Trace is one entry per state transition in Burr's
native format, suitable for replay through Burr's UI or for cross-
reference with the in-memory history.

### History entry shape

Each `burr://history` entry:

```jsonc
{
  "seq": 0,
  "ts": "2026-05-19T15:21:33.456+00:00",
  "action": "take_order",
  "inputs": {"item": "latte", "qty": 1},
  "state_after": {"stage": "ordered", "item": "latte", "qty": 1},
  "valid_next_actions": ["pay"],
  "refused": false,
  "refusal_reason": null
}
```

Refused attempts appear in the same list with `refused: true` and one
of five `refusal_reason` values:

- `invalid_transition`: the requested action isn't reachable from
  current state. `valid_next_actions` lists what is.
- `unknown_action`: the action name isn't in the graph.
- `action_error`: the action's wrapped function raised during
  execution. The entry also carries `error_type` and `error_message`
  so the client can distinguish "the FSM said no" from "the action's
  code blew up." State is not advanced.
- `action_timeout`: the action exceeded `action_timeout_seconds` and
  was cancelled. The entry carries `error_type: "TimeoutError"` and
  the configured timeout. State is not advanced.
- `validation_failed`: an input validator declared on the action
  raised `ValidationFailed`. The entry carries the reason in
  `error_message` and any structured `details` from the validator.
  State is not advanced.

Anyone with the history can replay the session or audit it without
filesystem access to Burr's tracker output.

## Install

Currently a private repo. From source:

```bash
git clone git@github.com:msradam/burr-mcp.git
cd burr-mcp
uv sync
```

Python 3.11 through 3.13.

## Examples

The `examples/` directory has eight self-contained servers covering
the patterns most people will hit. Each is runnable as
`uv run python examples/<file>.py` and can be wired into Claude Code
via the snippet in `examples/claude-code.example.json`.

| File | Pattern | Notes |
|---|---|---|
| `coffee_order.py` | Linear FSM | Smallest interesting example: `take_order → pay → fulfill`. |
| `triage.py` | Branching FSM | Classify input, then route to one of three branches based on the result. |
| `subgraphs.py` | Sub-Application composition | Parent action spawns a sub-FSM via `spawn_subapp`; nested timeline at `burr://subruns/{id}`. |
| `parallel_research.py` | Parallel fan-out | Research agent over a markdown corpus organised into per-source subfolders. `research(query, corpus_dir=None)` defaults to the shipped corpus at `examples/data/parallel_research/{services,runbooks,faqs}/` but accepts any directory the caller points it at (supports `~` and relative paths). Parent action fans out one search sub-Application per source via `asyncio.gather`; each sub-app runs a four-step pipeline (`load_documents → score_documents → extract_snippets → summarize`). Pure-Python term-frequency search, no external deps. Each sub-run is its own subrun with the source as label. |
| `granite_oncall.py` | LLM call inside the graph | On-call alert triage where two nodes call a real Granite model via Ollama. FSM provides the structure around the LLM: malformed outputs trigger retry-as-transition (max 3) before escalating to `route_to_human`. Requires `ollama serve` with `granite4.1:3b` pulled. |
| `unix_health.py` | Deterministic ops checks via shellouts | System-health FSM that runs real `df`, `uptime`, `ps`, and `vm_stat` (macOS) or `free` (Linux) via `asyncio.create_subprocess_exec` and parses each tool's stdout. Triages overall severity, branches to clean report or critical alert with deep-dive detail (top mounts, top processes by RSS or CPU, zombie reaper hunt) on the worst subsystem. Raw stdout per check is recorded in `state.raw_outputs` so `burr://state` shows what an operator would have seen at the terminal. macOS + Linux (WSL on Windows). No LLM. |
| `codebase_security.py` | Vulnerability audit with patch-overlay loop | Runs `bandit` + `detect-secrets` against a shipped vulnerable Python repo (`examples/data/codebase_security/vuln_demo/`), normalizes findings to a common schema with CWE IDs, severity, and per-CWE remediation hints. Remediation is search/replace patches recorded in state and applied to a tmpdir overlay on rescan; the original codebase on disk is never modified. Loop caps at 3 rounds; escalates when ≥2 critical/high findings persist across a rescan. |
| `adaptive_crag.py` | Self-correcting RAG | Granite-graded RAG. `ask(question, corpus_dir=None)` defaults to the shipped parallel_research corpus but accepts any markdown directory. After retrieval and synthesis, a grader scores the answer 1-5 on grounding + relevance; bad grade rewrites the query and loops back to retrieval. Cap at 3 rounds. Based on CRAG ([Yan et al 2024](https://arxiv.org/abs/2401.15884)) with a simplified LLM-as-judge instead of a trained T5 evaluator. |
| `skill_security_audit.py` | SKILL-to-FSM (caller LLM is the brain) | Real Claude Code SKILL (the MIT-licensed `examples/skills/security-audit/SKILL.md`) decomposed into a Burr FSM whose actions emit structured prompts for the caller LLM (Opus, Sonnet, Granite, whatever drives the MCP client). No server-side LLM, no scanners. Mode-branching on INSIDE / OUTSIDE / BOTH; OUTSIDE / BOTH require an `authorization_source` per the SKILL's written-authorization rule. Six phases gated as transitions. Four more skills ship in `examples/skills/` as reference for future conversions. |
| `mellea_qiskit_migration.py` | Mellea-in-a-Burr-node | A real Mellea sample mirrored. One action (`mellea_repair_loop`) calls Mellea's `session.instruct` with a deterministic Qiskit-1.0 migration checker as the `validation_fn`; Mellea runs its internal generate-validate-repair loop and returns the chosen sample plus a per-attempt validation log. The FSM owns the workflow (accept input, branch on success/giveup) and the audit trail; Mellea owns the IVR loop. Migration patterns inlined from Qiskit's own deprecation guide so the demo runs without `flake8-qiskit-migration` installed. Pre-req for real runs: `pip install mellea` + Ollama with `granite4:micro` pulled. |
| `streaming_narrate.py` | Streaming action | An action that yields intermediate chunks; each becomes an MCP progress notification, the final state arrives in the tool response. |
| `with_otel.py` | OpenTelemetry spans | Burr's `OpenTelemetryBridge` wired into the factory; every action run emits a span. Console exporter for demo; swap for OTLP/Jaeger in production. |
| `incident_response.py` | Showcase | Realistic ops workflow with all features (validators, sub-graphs, branching, conditional loop). The canonical Claude Code demo. |
| `git_review.py` | CLI wrapping | An FSM whose actions wrap `git status` / `log` / `show` via subprocess. Demonstrates the "agent driving CLIs" pattern with FSM-enforced sequence. |
| `adventure.py` | State-space traversal | Tiny text adventure where rooms are states and moves are gated transitions. Mirrors Burr's `llm-adventure-game`. Sharpest illustration of FSM-as-API. |
| `http_serve.py` / `sse_serve.py` | Transports | Same coffee FSM served over Streamable HTTP / SSE. |

For more FSM patterns to draw from, [Burr's example library](https://github.com/apache/burr/tree/main/examples)
has 30+ Applications covering chatbots, RAG pipelines, ML training
orchestration, recursive agents, and parallelism. Most of them can be
mounted via `burrmcp.mount(...)` without modification, and the
audit/transition/validator surface comes along for free.

## Try it with Claude Code

The included `examples/incident_response.py` is a realistic ops
workflow: an on-call engineer (or the agent) walks an incident from
report to postmortem, with the server enforcing the order
(`report → acknowledge → investigate → mitigate → verify →
resolve → write_postmortem`), validating that severity is one of
P1/P2/P3, and delegating the investigation step to a sub-graph whose
timeline is addressable at `burr://subruns/{id}`.

To wire it into Claude Code:

1. Copy `examples/claude-code.example.json` to `.mcp.json` in any
   project you'd like to test from, and edit the absolute path under
   `args[1]` to match your checkout of this repo.
2. Run `claude` from that project. Use `/mcp` to confirm the
   `incident-response` server connected.
3. Try a happy-path prompt:

   > Open a P1 incident: API 500s started 5 minutes ago, reporter
   > is alice. Then acknowledge as bob. Then run an investigation.
   > Then read `burr://subruns` and tell me what the investigation
   > found. Then mitigate by rolling back deploy 89a3. Verify the
   > mitigation worked. Resolve the incident. Write a one-paragraph
   > postmortem.

4. Try a refusal-path prompt:

   > Open a Sev-2 incident: alerts firing on the queue worker.

   Severity `Sev-2` isn't P1/P2/P3, so the validator refuses and
   returns the legal values. The model should retry with `P2`.

5. Try an out-of-order prompt:

   > Resolve incident INC-99 with resolution "rolled back".

   No incident is open yet (FSM is at `report`), so the call comes
   back as `invalid_transition` with `valid_next_actions: ["report"]`.

The `burr://history` resource records every attempt, refusals and
successes alike, so you can ask the agent: "show me the audit trail"
and it'll read the resource and summarise.

## Run other examples

```bash
uv run python examples/coffee_order.py
```

That starts the FastMCP server in stdio mode. Wire it into any MCP
client with a config that points at the command.

For HTTP deployments:

```bash
uv run python examples/http_serve.py            # binds 127.0.0.1:8765
BURR_MCP_PORT=9000 uv run python examples/http_serve.py
```

Connect with a FastMCP client by URL:

```python
from fastmcp import Client
async with Client("http://127.0.0.1:8765/mcp") as client:
    await client.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
```

`tests/test_http_transport.py` spawns this example as a subprocess
and connects two concurrent HTTP clients to verify session isolation
on the wire format.

To drive it from a local LLM with [mcphost](https://github.com/mark3labs/mcphost),
copy `examples/mcphost.example.json` somewhere, edit the absolute path
to match your checkout, then:

```bash
mcphost --config /path/to/mcphost.json -m ollama:granite4.1:3b -p \
  'Place a coffee order. Call step with action=take_order and inputs={"item":"latte","qty":1}, then step with action=pay and inputs={"amount":5.5}, then step with action=fulfill and inputs={}.'
```

The server returns the new state and `valid_next_actions` after each
call. Try `pay` before `take_order` and the server returns
`invalid_transition` with `valid_next_actions: ["take_order"]`.

## CLI

`pip install burrmcp` (or `uv sync`) registers a `burrmcp` console
script. Launch any importable Application or factory:

```bash
burrmcp serve coffee_order:build_application --mode step
burrmcp serve triage:build_application --mode dynamic --name triage
```

The first argument is a `module:attr` target, the same shape uvicorn
and gunicorn use. The attr may be a built `Application` or a callable
returning one.

`burrmcp doctor` runs static validation against the same target
before you mount it. Catches the failure modes that only surface at
runtime: unreachable actions, factory exceptions, dead-end terminals,
state keys read before anything writes them, orphan initial state.

```bash
burrmcp doctor coffee_order:build_application --app-dir examples
```

Exit code is `0` when there are no failures (warnings and info notes
don't block) and `1` otherwise, so a `burrmcp doctor` invocation
slots into CI. Importable from Python too: `from burrmcp.doctor
import run_checks`.

## Branching example

`examples/triage.py` shows conditional transitions: after `classify`
writes `severity` into state, the graph branches into `escalate`,
`queue`, or `drop`. `burr://next` returns only the branch matching
current state; calling the wrong branch is refused with the right one
listed in the response.

```bash
burrmcp serve triage:build_application
```

## More self-contained demos

`examples/chargen.py` is a six-stage D&D-style character builder:
`begin -> choose_race -> choose_class -> assign_stats -> pick_skills
-> equip -> finalize`. Each step writes one slice of the character
sheet and unlocks exactly one next step. Prompt your MCP client with
"just finalize my character" and watch the FSM refuse, pointing at
`begin` as the only legal next move.

`examples/release_pipeline.py` is the canonical "agent refuses to
skip-ahead" demo: a deploy pipeline that gates promotion behind tests,
canary deployment, and at least two healthy canary observations. The
demo line is "just promote this hotfix to prod". `step` returns
`invalid_transition` with `valid_next_actions: ["submit_change"]`, and
the agent has to walk through the gates instead. A degraded canary
observation forces `rollback`; only `rollback` is callable, not
`promote_to_prod`.

`examples/local_shell.py` is a tiny Claude-Code-style local-shell
agent with four safety rules baked into the FSM: you cannot
`edit_file` a path you haven't `read_file`'d first, you cannot
`create_file` over a path that already exists or is already
pending, you cannot `commit` unless tests passed since the last
edit (or create), and deletion is two-step (`request_delete` then
`confirm_delete`). None of these rules are written in the agent's
prompt; the server refuses unsafe sequences. Demo line: "edit
main.py to print goodbye". A naive agent goes straight to
`edit_file`, the server returns
`"must read 'main.py' before editing it. Files read so far: []"`,
and the agent self-corrects. Another demo line: "create main.py
with new contents". `create_file` refuses because main.py already
exists in workspace; the agent rewires to read-then-edit.

All three work with no external dependencies. Wire them into Claude
Code via `examples/claude-code.example.json`.

`examples/granite_oncall.py` puts a real LLM call inside the graph.
An on-call alert text comes in; the FSM runs two Granite calls
(via Ollama) sandwiched around a deterministic corpus lookup:
`report_alert -> classify_severity -> extract_service ->
suggest_runbook -> format_response`. Each LLM step auto-retries up
to three times on malformed output (severity not in `{P0,P1,P2,P3}`,
service not in the known list), and after three strikes the FSM
routes to a `route_to_human` terminal action with every Granite
attempt captured in state so an operator can see what the model
was saying. The retry loop is encoded as transitions, so each
attempt is its own visible step in `burr://history` and the trace,
not buried in a Python `while`. Requires Ollama running with
`granite4.1:3b` pulled; tests monkey-patch the Granite call so
they stay hermetic.

`examples/unix_health.py` is a deterministic Unix system-health
FSM that shells out to the canonical Unix tools an ops engineer
would actually type: `df -k /` for disk, `vm_stat` (macOS) or
`free -b` (Linux) for memory, `uptime` for load average, and
`ps -A -o pid,stat,pcpu,comm` for processes. Each tool runs via
`asyncio.create_subprocess_exec`; raw stdout per check is recorded
in `state.raw_outputs` so `burr://state` shows the literal terminal
output. The FSM computes overall severity as the max of the
per-check statuses, then branches: a healthy or warning system
goes straight to `produce_report`; a critical system routes
through `deep_dive` (which runs an additional shellout for the
worst subsystem, e.g. `df -k` for all mounts or `ps -A -o pid,rss,comm`
sorted by RSS) into `raise_alert`. The triage node refuses the
wrong terminal: `produce_report` is unreachable when any check is
critical, and `deep_dive` is unreachable when nothing is critical.
macOS and Linux supported; Windows via WSL.

`examples/codebase_security.py` is the vulnerability-audit FSM.
Points at a Python repo, runs real `bandit` and `detect-secrets`
subprocesses, normalizes every finding to a common schema with CWE
ID, severity, file:line, offending snippet, and a per-CWE
remediation hint. The remediation loop is the interesting bit: the
agent calls `propose_patch(file_path, search, replace)` one or more
times (every patch is recorded in `state.patch_log` and visible in
`burr://history`), optionally `acknowledge_finding` to suppress
known-OK findings, then `confirm_fixes_applied` to trigger a
rescan. The rescan copies the repo to a tmpdir, applies every
recorded patch via literal string replace, runs the scanners
against the overlay, and cleans the tmpdir up. The original
codebase on disk is never modified, so the demo is re-runnable
indefinitely. A stuck-counter forces escalation when ≥2 critical or
high findings persist across a rescan; the budget caps at 3 rounds.
Ships with a deliberately-vulnerable Python micro-app at
`examples/data/codebase_security/vuln_demo/` covering CWE-89
(SQL injection), CWE-78 (command injection), CWE-502 (insecure
deserialization), CWE-95 (eval), CWE-327 (weak crypto), and CWE-798
(hardcoded credentials).

`examples/adaptive_crag.py` is self-correcting RAG over the
`parallel_research` corpus. After retrieval and Granite synthesis,
a second Granite call grades the answer 1-5 on grounding +
relevance. A bad grade prompts a third Granite call to rewrite the
search query, and the FSM loops back to retrieval. Cap at 3 rounds.
Implements a simplified version of the CRAG pattern from
[Yan et al 2024](https://arxiv.org/abs/2401.15884), with two
simplifications: the trained T5 evaluator is replaced by an
LLM-as-judge, and the corrective branch is a query rewrite rather
than an external web fallback.

`examples/skill_security_audit.py` takes a real Claude Code SKILL
(the MIT-licensed web-app security audit at
`examples/skills/security-audit/SKILL.md`) and decomposes its
phases into a Burr FSM whose actions emit prompts for the *caller*
LLM. No server-side LLM call, no scanners. Whoever is driving
BurrMCP through MCP (Sonnet, Opus, Granite, whatever) processes
the prompts; the FSM enforces order. Six phases: context detection
-> source review (INSIDE / BOTH only) -> blackbox review (OUTSIDE /
BOTH only, requires `authorization_source` per the SKILL's "you
need written authorization" rule) -> infra sweep -> rate-limit
deep-dive -> write_advisory (terminal). The pitch: the SKILL was
unstructured markdown; the FSM makes its order verifiable, every
phase a visible step in `burr://history`, and the audit trail of
prompts plus the agent's structured findings is the artifact.
Complementary to `codebase_security.py` (real scanners against a
vulnerable demo repo): that one is "scanners find findings", this
one is "agent applies a SKILL under FSM-enforced order". Four more
skills (`claude-api`, `mcp-builder`, `webapp-testing`,
`skill-creator`) ship in `examples/skills/` as reference material
for future SKILL-to-FSM conversions.

`examples/mellea_qiskit_migration.py` integrates IBM Research's
[Mellea](https://github.com/generative-computing/mellea) library
as a single Burr action. Mellea is a generative-programming
library whose `session.instruct` primitive runs an internal
generate-validate-repair loop against natural-language requirements
with deterministic `validation_fn` hooks; this demo mirrors
Mellea's own
[qiskit_code_validation](https://github.com/generative-computing/mellea/blob/main/docs/examples/instruct_validate_repair/qiskit_code_validation/qiskit_code_validation.py)
sample. The FSM hands Mellea pre-Qiskit-1.0 code (uses
`IBMQ.load_account()`, `execute(circuit, backend)`,
`Aer.get_backend(...)`, `QasmSimulator()`); the deterministic
checker validates each Mellea sample against the Qiskit 1.0
deprecation patterns; Mellea repairs until clean or the budget
exhausts. The FSM then routes to `finalize_success` or
`finalize_giveup` based on a canonical re-check on the chosen
sample. Mellea owns the IVR loop; Burr owns the workflow and the
audit trail visible in `burr://state` and `burr://history`. Mellea
is lazy-imported inside `_call_mellea` so the example module is
importable without Mellea installed; tests monkey-patch the
wrapper for hermetic runs.

## Tests

```bash
uv run pytest
```

Three hundred and thirty-three tests in about 16 seconds (real bandit + detect-secrets subprocess scans in the codebase_security tests account for most of the runtime; the rest of the suite is in-process and lands in well under a second). Most use FastMCP's in-process
client; `tests/test_http_transport.py` spawns the HTTP example as a
subprocess and drives it with two real HTTP clients.
`tests/test_hardening.py` covers action exceptions, concurrent steps
within one session, and non-JSON state coercion.
`tests/test_importing.py` covers the lift-a-flat-server path: sync
and async tools, branching transitions, signature preservation, and
the `only`/`rename`/`state_update` options.
`tests/test_timeouts.py` covers the action-timeout knob: slow actions
get cancelled, fast ones pass through, no-timeout leaves slow work
alone, and timeouts apply in TOOLS mode too.
`tests/test_trace.py` covers the tracker passthrough: no-tracker
error, post-step content, path resolution, traversal safety.
`tests/test_per_action_timeout.py` covers the per-tool override:
ToolSpec timeout wins over the server default, applies when there's
no server default, inheriting when no override is set, and the
hand-tagged escape hatch for non-importer Burr actions.

## Design notes

### Why three modes

MCP clients handle mid-conversation tool list changes differently.
Rather than pick one shape and live with the trade-off, BurrMCP
exposes three:

- `TOOLS`: closest in shape to existing MCP servers. The graph is
  advisory; the client can call any action at any time. Useful when
  you want Burr's state contract and tracker without imposing
  transitions on the client.
- `STEP`: one meta tool, transitions enforced server-side, every
  client works the same way. The model has to learn one indirection
  (which action name to pass) instead of seeing each action as its
  own tool.
- `DYNAMIC`: per-action tools where visibility tracks the current
  state via FastMCP tag-based enable/disable. The model sees only
  the tools it can actually call right now. Requires the client to
  honor `tools/list_changed` notifications.

All three share the same resources and the same Burr tracker output
under `~/.burr/`.

### Per-session isolation

`mount(...)` accepts either an `Application` instance or a callable
factory. Instance: one shared FSM across all sessions, mutated in
place. Factory: each MCP session gets its own Application, built
lazily on first tool call and keyed by `ctx.session_id` in a
per-server dict.

```python
# Shared state across sessions (single-user local tooling).
server = mount(app, mode=ServingMode.STEP)

# Per-session isolation (multi-user server, web-mounted MCP, etc.).
server = mount(build_application, mode=ServingMode.STEP)
```

FastMCP 3.2's built-in `ctx.set_state(serializable=False)` is
**request-scoped**, not session-scoped, so it's not suitable for
caching the Application across calls in one session. The session
store is a plain dict held in `mount`'s closure scope, with lazy
TTL + max-size eviction.

```python
mount(
    build_application,
    mode=ServingMode.STEP,
    session_ttl_seconds=3600,  # evict after 1 hour idle (default)
    max_sessions=100,          # evict LRU when this many live (default)
)
```

Set either knob to `None` to disable that form of eviction. Eviction
is lazy: stale entries are dropped on the next access, not on a
background timer. The unit tests in `tests/test_eviction.py` cover
TTL drop, max-size drop, and the post-eviction "fresh Application
on next call" behavior.

### What this is not

- Not a Burr replacement. Burr handles state, graphs, and tracking;
  this just bridges to MCP.
- Not opinionated about model loops. The agent decides what to call;
  `burrmcp` decides whether the call is allowed.
- Not a workflow engine. No retries, no durability, no scheduling.
  Use Temporal if you need those.

## Roadmap

Shipped in v0.1.0:

- Per-session isolation via factory mode.
- Branching example (conditional transitions).
- `burrmcp serve module:attr` CLI.
- CI on 3.11/3.12/3.13.

Shipped in v0.2.0:

- `burr://history` resource: per-session audit trail of every action
  attempt (successes and refusals).
- DYNAMIC mode now uses `ctx.enable_components` / `ctx.disable_components`
  for true per-session visibility. Concurrent sessions see independent
  tool lists, verified by `tests/test_dynamic_per_session.py`. The
  earlier version used the server-wide `mcp.enable`/`mcp.disable`,
  which leaked visibility across sessions.

Shipped in v0.3.0:

- Session-store eviction: TTL + max-size, lazy, configurable via
  `mount(..., session_ttl_seconds=..., max_sessions=...)`. Defaults
  to 3600 seconds and 100 sessions.
- `examples/http_serve.py`: Streamable HTTP example.
- `tests/test_http_transport.py`: spawns the HTTP example as a
  subprocess and drives it with two concurrent HTTP clients to
  verify per-session isolation on the wire format.

Shipped in v1.9.0:

- Every tool response (step, reset_session, fork_at, fork_from_past)
  now carries an `app_id` field. Clients tracking sessions across
  server restarts have a stable id to remember without fishing it
  out of `burr://trace`.
- `fork_from_past` generalized to support any Burr `BaseStateLoader`,
  not just `LocalTrackingClient`. Pass `state_loader=...` to `mount()`
  and resume works against custom SQLite/postgres/S3 persisters.
  Three-tier source resolution: explicit loader wins, then the
  Application's `LocalTrackingClient` if present, then refuse.
- `fork_from_past` accepts an optional `partition_key` parameter for
  persisters that use partitioned storage.

Shipped in v1.8.0:

- `fork_from_past(app_id, sequence_id)` meta-tool. Loads a persisted
  Burr run from disk and rewinds the session to that state. Lets an
  agent resume a session across server restarts (track the app_id
  on the client, restore here after reconnect) or fork from any
  past persisted run, not just the current session's in-memory
  history. Requires the Application to have a `LocalTrackingClient`
  attached (the same setup that powers `burr://trace`). Refuses
  cleanly when the app_id doesn't exist on disk, when no tracker is
  attached, or in shared-app mode.
- `fork_from_past` joins `reset_session`, `fork_at`, and the others
  in `burr://graph`'s `meta_tools` advertisement so a connecting
  agent discovers it during cold-start.

Shipped in v1.7.0:

- OpenTelemetry observability via Burr's `OpenTelemetryBridge`
  lifecycle adapter. Wire it into your Application factory with
  `.with_hooks(OpenTelemetryBridge(tracer_name=...))` and every
  action run emits a span. Works transparently through the MCP wire:
  spans from `step`, `spawn_subapp`, streaming actions, `fork_at`,
  all flow through the same bridge.
- New `[observability]` install extra: `pip install
  'burrmcp[observability]'` pulls Burr's opentelemetry extra plus
  the core OTel API/SDK.
- `examples/with_otel.py` demonstrates the full wire-up with a
  console span exporter. Swap the exporter for OTLP/Jaeger/Honeycomb
  by changing one line.

Shipped in v1.6.0:

- Streaming Burr actions plumbed through to MCP progress
  notifications. When an action is decorated with
  `@streaming_action`, the adapter detects it (via the
  `action.streaming` attribute) and uses `app.astream_result` instead
  of `astep`. Each yielded chunk is forwarded to the client via
  `ctx.report_progress` (the MCP-spec mechanism for partial results
  during a long-running tool call); the final state arrives in the
  regular tool response with `streamed: true` and a `chunks` count.
- `examples/streaming_narrate.py`: a streaming narration action that
  yields chunks of a generated story. Works with any client that
  honours progress notifications (Claude Code does).
- Clients that don't supply a progress token still get the final
  result; intermediate chunks are dropped silently. The streaming
  path stays robust to that.

Shipped in v1.5.0:

- Parallel sub-Application spawn works without any new code in the
  adapter. ``spawn_subapp`` intentionally doesn't acquire the
  session lock, so an action body can call it inside
  ``asyncio.gather`` to fan out: N sub-applications run concurrently,
  each becomes its own ``burr://subruns/{id}`` entry, and the parent
  history entry lists every spawned id under ``subruns``.
  ``examples/parallel_research.py`` is the canonical pattern. The
  timing test proves five 50ms sub-apps complete in under 200ms
  rather than the 250ms a serial walk would take.

Shipped in v1.4.0:

- `fork_at(sequence_id)` meta-tool. Rewind the session's Application
  to the state captured after any prior history entry, letting an
  agent explore alternate paths without disconnecting. Implemented
  via the in-memory `burr://history`, so it works without users
  having to wire up a Burr `LocalTrackingClient`. Refuses to fork to
  a refusal entry, to a meta-tool entry (would be a hall of mirrors),
  or in shared-app mode.
- Typed state schemas in `burr://graph`. If the Application is built
  with Burr's `PydanticTypingSystem`, the full state JSON schema
  (one Pydantic model) is surfaced under `state_schema` so an MCP
  client gets the typed shape without inspecting each action's
  writes. Untyped state shows `state_schema: null`.
- Discovery hint in server instructions now mentions both
  `reset_session` and `fork_at` so an agent learns both escape
  hatches at cold start.

Shipped in v1.3.0:

- `reset_session` MCP tool. Always callable regardless of FSM state.
  In factory mode, rebuilds the session's Application via the factory,
  clears any spawned sub-runs, and appends a `reset_session` marker
  to history (prior entries preserved, so the audit trail shows the
  reset rather than wiping it). In shared-app mode, refuses with a
  structured `reset_not_supported` error explaining why. Surfaced in
  `burr://graph` under a new `meta_tools` field so a connecting agent
  discovers the escape hatch during cold-start.
- The cold-start discovery hint in server instructions now mentions
  `reset_session` so agents know to call it after reaching a terminal
  state or a dead-end branch, instead of asking the human to restart
  the server.

Surfaced by a real Claude Code session: an agent that walked the
adventure to victory wanted to try the alternate path, correctly
diagnosed that the FSM was terminal, and asked whether the server
exposed a reset mechanism. It didn't. Now it does.

Shipped in v1.2.0:

- Two new examples for the quickstart library:
  - `examples/git_review.py`: CLI-wrapping FSM that runs real `git`
    commands via subprocess and forces a useful inspection sequence
    (`status → recent_commits → show_commit → summarize`). The
    canonical example of the "wrap CLIs as gated actions" pattern.
  - `examples/adventure.py`: text adventure with rooms as states
    and inventory-gated moves, mirroring Burr's `llm-adventure-game`.
    The FSM-as-API pitch at its sharpest.
- Sharpened README opening to lead with the one-line definition
  ("an adapter that turns a Burr state machine into an MCP server")
  and a new `## Examples` table indexing all eight examples.
- Bug fix: `_step_application` now forces Burr to execute the
  specifically-requested action via a one-call override of
  `app.get_next_action`. Previously, in branching graphs where two
  transitions from the same source both satisfied their conditions,
  Burr's `astep` ran whichever was listed first regardless of what
  the client asked for. The adventure example surfaced it; the fix
  is contained and tracker hooks still fire normally through Burr's
  regular `_astep` machinery.

Shipped in v1.1.0:

- `burr://graph` resource. Static description of the FSM topology
  (actions with their reads/writes/inputs/docstring, transitions with
  their conditions). Computed once at mount time. Lets a connecting
  model learn the whole graph in one read instead of inferring it
  from trial-and-error or per-tool docstrings.
- Server-level `instructions` now include a one-line discovery hint
  pointing at `burr://graph`, so the model sees it before its first
  tool call. User-supplied `instructions` are preserved alongside.

Shipped in v1.0.1:

- `burr://subruns` index entries now include a fully-rendered ``uri``
  field (e.g. ``burr://subruns/sub-abc...``) so consumers don't have
  to construct the URI from the template.
- History entries that spawned sub-runs now carry both ``subruns``
  (bare ids) and ``subrun_uris`` (rendered URIs) so a client reading
  ``burr://history`` can follow cross-references without inference.

Shipped in v1.0.0:

- Subgraph mounting. A Burr action body can call
  `await burrmcp.spawn_subapp(sub_app, label=...)` to delegate a
  multi-step procedure to a sub-Application. The sub-run's timeline
  is recorded under the parent session and addressable via two new
  MCP resources: `burr://subruns` (index) and
  `burr://subruns/{id}` (full record). The parent action's history
  entry carries the new subrun ids under a `subruns` field so a
  client can correlate parent action with child timeline.
- The session entry now also tracks a ContextVar so spawn_subapp
  knows which session it's running inside without callers having to
  thread context manually.
- `examples/subgraphs.py` shows the pattern end-to-end with a
  three-step investigation sub-graph spawned from a parent FSM.

Shipped in v0.9.0:

- Input validators. A callable that runs between MCP arrival and
  action execution; can refuse the call with `ValidationFailed`,
  return a dict to substitute normalised inputs, or return None to
  accept the originals. Sync and async both supported. Three ways to
  declare:
  - `mount(input_validators={"action_name": fn, ...})` server-wide.
  - `ToolSpec(validator=fn)` per-tool via the importer.
  - `fn._burrmcp_validator = callable` on a hand-written Burr action.
  Refusals show up as `error: "validation_failed"` on the wire and
  `refusal_reason: "validation_failed"` in history, with the validator's
  reason and details preserved.

Shipped in v0.8.0:

- `examples/sse_serve.py`: serve over the older SSE transport for
  clients that don't yet support Streamable HTTP.
- Per-action timeout overrides. `ToolSpec(timeout_seconds=N)` in the
  importer applies a timeout to that action only, regardless of the
  server-wide `action_timeout_seconds`. Hand-written Burr actions can
  opt in by setting `fn._burrmcp_timeout_seconds = N` on the
  decorated function.

Shipped in v0.7.0:

- `burr://trace` resource: read-through of Burr's on-disk
  `LocalTrackingClient` log for the current session's Application.
  Closes the cross-reference gap between BurrMCP's in-memory
  `burr://history` and Burr's own structured trace format. Capped at
  1000 most-recent records to keep the wire payload bounded. Path
  resolution is safe against `app.uid` containing traversal sequences.
  Requires the Application to have been built with
  `.with_tracker(LocalTrackingClient(project=...))`; otherwise
  returns a `no_tracker` error explaining how to enable it.

Shipped in v0.6.0:

- Action timeouts. `mount(..., action_timeout_seconds=N)` wraps every
  action invocation in `asyncio.wait_for`. On expiry the coroutine is
  cancelled, the call returns `{"error": "action_timeout"}` to the
  client, the timeout is recorded in `burr://history` with
  `refusal_reason: "action_timeout"`, and the FSM does not advance.
  Default is `None` (no timeout, original behavior). Cancellation is
  prompt for async I/O work; best-effort for sync or CPU-bound work.

Shipped in v0.5.0:

- `burr_app_from_fastmcp(...)` importer: lift an existing FastMCP
  server's tools into a Burr Application by declaring per-tool
  reads/writes plus the legal transitions. The result re-mounts via
  `mount()` like any other Burr Application, gaining transition
  enforcement, audit history, per-session isolation, eviction, and
  everything else BurrMCP provides.
- `ToolSpec` dataclass for the per-tool declarations:
  `reads`/`writes`/`merge_result`/`state_update`/`rename`.
- `tests/test_importing.py` covers the full importer surface end-to-end
  (state-mutation declaration, conditional transitions, rename, the
  rejection of duplicate-tool registration, signature preservation
  across async/sync).

Shipped in v0.4.0 (hardening for frontier-model deployments):

- Action exceptions are captured. If an action's wrapped function
  raises, the adapter wraps it as `ActionExecutionError`, returns a
  structured `{"error": "action_error", "error_type": "...",
  "error_message": "..."}` to the client, records the same shape in
  `burr://history` with `refusal_reason: "action_error"`, and does
  not advance state. The session stays at its prior position.
- Per-session `asyncio.Lock` around `app.astep`. Burr Applications
  are not thread-safe and the MCP protocol permits parallel tool
  calls within one session; the lock serialises them. Different
  sessions still proceed in parallel.
- Non-JSON-serialisable state values are coerced to strings rather
  than silently breaking the resource. The state response surfaces
  affected keys under `_burrmcp.coerced_keys` so the client knows
  the round-trip is lossy.
- Burr pinned to `>=0.40.2,<0.41` since we rely on internal API
  surface (`Action.fn`, `Action.inputs` tuple shape, `__PRIOR_STEP`).

Shipped in v1.10.0:

- `burrmcp doctor module:attr` CLI subcommand for static validation
  before mounting. Checks: target resolves, factory builds, every
  action is reachable from the entrypoint, terminal nodes are
  surfaced, every state-key read has a writer or initial seed, orphan
  initial keys are flagged. Importable as
  `from burrmcp.doctor import run_checks` for use in tests too.

Next (v1.x):

- Public PyPI release.
- WebSocket transport example.
- Optional Pydantic-model output schemas surfaced as MCP tool
  `outputSchema` for stronger client contracts.

## License

Apache 2.0.
