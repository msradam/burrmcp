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

## Sessions

`mount(...)` takes either a built `Application` (shared across all clients) or a callable `() -> Application` (each MCP session gets its own state). Factory mode is the default for multi-client servers; instance mode is fine for single-user local tooling. Sessions get TTL + LRU eviction and per-session locks so concurrent calls don't race on Burr's not-thread-safe `Application`. An action can call `spawn_subapp(sub_app)` and the nested timeline becomes addressable at `burr://subruns/{id}`.

Refusals come in five structured classes (`invalid_transition`, `unknown_action`, `action_error`, `action_timeout`, `validation_failed`) and are all recorded in `burr://history`.

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

`examples/` ships self-contained FSMs across these patterns. Each is runnable as `uv run python examples/<file>.py` and can be wired into Claude Code via `examples/claude-code.example.json`.

**Pure FSM, no external deps:**

| File | Pattern |
|---|---|
| `coffee_order.py` | Linear FSM: `take_order -> pay -> fulfill`. |
| `triage.py` | Branching FSM with conditional transitions. |
| `adventure.py` | State-space traversal. Rooms are states, moves are gated transitions. |
| `chargen.py` | Sequential narrowing wizard with strict ordering. |
| `release_pipeline.py` | Agent refuses to skip-ahead. Gated tests/canary/promote. |
| `local_shell.py` | Read-before-edit safety rails; patch-overlay via state. |
| `incident_response.py` | Realistic ops workflow with all features. Canonical Claude Code demo. |
| `subgraphs.py` | Sub-Application composition via `spawn_subapp`. |
| `ml_training.py` | Non-LLM iterative training (pure stdlib logistic regression). |
| `streaming_narrate.py` | Streaming actions as MCP progress notifications. |
| `skill_security_audit.py` | SKILL-to-FSM. Caller LLM is the brain, no server-side LLM. |
| `differential_review.py` | Trail of Bits' differential-review SKILL as a 7-phase FSM. Pre-analysis -> triage -> code analysis -> test coverage -> blast radius -> (HIGH-risk only) deep context -> adversarial -> report. Risk-level gate enforced at the transition layer. |
| `fp_check.py` | Trail of Bits' fp-check SKILL as an 8-phase FSM. Step 0 restate-claim is a hard precondition; six mandatory gate reviews (process, reachability, impact, PoC, math, environment) before TRUE / FALSE POSITIVE verdict. |

**Shellout / deterministic tooling:**

| File | Pattern |
|---|---|
| `unix_health.py` | Real `df` / `uptime` / `ps` / `vm_stat` shellouts with severity-branching diagnostics. |
| `codebase_security.py` | Real `bandit` + `detect-secrets` against a shipped vulnerable repo; patch-overlay loop. |
| `git_review.py` | Wraps `git status` / `log` / `show` via subprocess. |

**LLM-in-the-graph (server-side calls):**

| File | Pattern |
|---|---|
| `granite_oncall.py` | Granite via Ollama; retry-as-transitions for malformed output. |
| `adaptive_crag.py` | Granite self-grading RAG; query-rewrite loop. Simplified CRAG ([Yan et al 2024](https://arxiv.org/abs/2401.15884)). |
| `mellea_qiskit_migration.py` | Mellea's IVR loop wrapped as one Burr action. FSM owns workflow, Mellea owns the loop. |

**External-library coexistence and observability:**

| File | Pattern |
|---|---|
| `hamilton_features.py` | Hamilton driver inside a Burr action body. No special integration. |
| `burr_map_parallel.py` | Burr's native `MapStates` primitive; inline note documents the `RayExecutor` swap. |
| `sqlite_persister.py` | Custom `BaseStatePersister`; `fork_from_past` round-trips through SQLite. |
| `parallel_research.py` | `asyncio.gather` fan-out per source folder over a shipped markdown corpus. |
| `with_otel.py` | `OpenTelemetryBridge` wired into the factory; spans for every action. |
| `http_serve.py` / `sse_serve.py` | Same coffee FSM served over Streamable HTTP / SSE. |

**Composed-use showcase:**

| File | Pattern |
|---|---|
| `combinatoric_testing.py` | Hamilton DAG + Burr FSM + BurrMCP together for LLM-driven numeric parameter search. Two percentile implementations as the SUT (differential testing); the caller LLM hunts the input space for divergence-maximizing combos, with every trial a tracked Burr session and every interesting combo reproducible via `fork_from_past`. |
| `combo_testing.py` | Same architectural pattern, categorical inputs. A checkout pricing engine with four categorical dimensions and three seeded 2-way interaction bugs; the caller LLM hunts failing combos, and finalize tallies failures per (dimension, value) so the structure of the bugs surfaces in the summary. The textbook pairwise / N-wise combinatorial-testing shape. |

[Burr's own example library](https://github.com/apache/burr/tree/main/examples) has 30+ more Applications. Most mount via `burrmcp.mount(...)` unchanged; the compatibility matrix above is the source of truth on what's been exercised.

## Try it

Stdio (default), for Claude Code / mcphost / any spawning client:

```bash
uv run python examples/coffee_order.py               # direct
uv run burrmcp serve coffee_order:build_application  # CLI form
```

HTTP, for browser or remote clients:

```bash
uv run python examples/http_serve.py            # binds 127.0.0.1:8765
BURR_MCP_PORT=9000 uv run python examples/http_serve.py
```

Wiring into Claude Code: copy `examples/claude-code.example.json` to `.mcp.json` in any project, edit the `args[1]` absolute path to match your checkout, then run `claude` and `/mcp` to confirm. `examples/mcphost.example.json` is the mcphost-flavored equivalent.

A typical session against `incident_response`:

> Open a P2 incident, db latency spiking on shard 7. Reporter is alice.
> Acknowledge as bob. Run an investigation. Read `burr://subruns` and
> tell me what it found. Mitigate by rolling back deploy 89a3.
> Verify. Resolve. Write a one-paragraph postmortem.

Try `Resolve incident INC-99 with resolution "rolled back"` on a fresh session: the FSM is at `report`, so the call comes back as `invalid_transition` with `valid_next_actions: ["report"]`. The agent self-corrects from there.

To observe the FSM live from another terminal: `uv run burrmcp watch` tails `~/.burr/<project>/<app-id>/log.jsonl` for the most-recently-touched session and pretty-prints each step.

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

## License

Apache 2.0.
