# burr-mcp

**An adapter that turns a [Burr](https://burr.dagworks.io/) state
machine into an [MCP](https://modelcontextprotocol.io/) server.** Each
`@action` becomes a tool, state lives on the server, and an agent
connecting over MCP can only call actions reachable from the current
state. Calls to unreachable actions come back as structured errors
that list the actions that *are* reachable.

Or, in the other direction: take an existing flat FastMCP server,
declare which tools mutate which state keys and which transitions
are valid, and `burr_app_from_fastmcp(...)` lifts it into a Burr
Application that mounts the same way, gaining transition enforcement,
audit history, and per-session isolation.

Status: v1.4.0.

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
expose the whole machine as one opaque tool with arguments. `burr-mcp`
exposes each node of the graph as its own tool, and lets the server
decide which calls are valid given the current state. That difference
matters when the client is an LLM picking from a tool menu: the menu
is the graph, not a single black-box call.

## Three serving modes

```python
from burr_mcp import mount, ServingMode

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
from burr_mcp import ServingMode, ToolSpec, burr_app_from_fastmcp, mount

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
nature. See `examples/import_flat.py` for the full pattern.

`ToolSpec` also accepts `state_update` (an explicit callable taking
the tool's result and returning state mutations, overrides
`merge_result`) and `rename` (change the action's name in the Burr
graph, useful when merging multiple flat servers).

## Coffee in 30 lines

```python
from burr.core import action, ApplicationBuilder, State
from burr_mcp import mount, ServingMode

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

Every mounted server registers seven MCP resources:

| URI | Returns |
|---|---|
| `burr://graph` | Static description of the FSM topology: every action with its description, reads, writes, required/optional inputs; every transition with its condition. Computed once at mount time. **Read this first** to see the whole graph in one fetch. |
| `burr://state` | Current Application state as JSON. Internal Burr keys (`__PRIOR_STEP`, `__SEQUENCE_ID`) filtered. |
| `burr://next` | Action names reachable from the current state. Empty list after a terminal action. |
| `burr://history` | Per-session timeline of every action attempted (successes and refusals). |
| `burr://trace` | Burr's on-disk `LocalTrackingClient` log for the current session's Application. Capped at 1000 most-recent records. Returns `{"error": "no_tracker"}` if no `LocalTrackingClient` was attached. |
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
| `incident_response.py` | Showcase | Realistic ops workflow with all features (validators, sub-graphs, branching, conditional loop). The canonical Claude Code demo. |
| `git_review.py` | CLI wrapping | An FSM whose actions wrap `git status` / `log` / `show` via subprocess. Demonstrates the "agent driving CLIs" pattern with FSM-enforced sequence. |
| `adventure.py` | State-space traversal | Tiny text adventure where rooms are states and moves are gated transitions. Mirrors Burr's `llm-adventure-game`. Sharpest illustration of FSM-as-API. |
| `import_flat.py` | Reverse direction | Lift an existing flat FastMCP server into a Burr graph via `burr_app_from_fastmcp(...)`. |
| `http_serve.py` / `sse_serve.py` | Transports | Same coffee FSM served over Streamable HTTP / SSE. |

For more FSM patterns to draw from, [Burr's example library](https://github.com/apache/burr/tree/main/examples)
has 30+ Applications covering chatbots, RAG pipelines, ML training
orchestration, recursive agents, and parallelism. Most of them can be
mounted via `burr_mcp.mount(...)` without modification, and the
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

`pip install burr-mcp` (or `uv sync`) registers a `burr-mcp` console
script. Launch any importable Application or factory:

```bash
burr-mcp serve coffee_order:build_application --mode step
burr-mcp serve triage:build_application --mode dynamic --name triage
```

The first argument is a `module:attr` target, the same shape uvicorn
and gunicorn use. The attr may be a built `Application` or a callable
returning one.

## Branching example

`examples/triage.py` shows conditional transitions: after `classify`
writes `severity` into state, the graph branches into `escalate`,
`queue`, or `drop`. `burr://next` returns only the branch matching
current state; calling the wrong branch is refused with the right one
listed in the response.

```bash
burr-mcp serve triage:build_application
```

## Tests

```bash
uv run pytest
```

One hundred and thirty-four tests in about 3.9 seconds. Most use FastMCP's in-process
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
Rather than pick one shape and live with the trade-off, `burr-mcp`
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
  `burr-mcp` decides whether the call is allowed.
- Not a workflow engine. No retries, no durability, no scheduling.
  Use Temporal if you need those.

## Roadmap

Shipped in v0.1.0:

- Per-session isolation via factory mode.
- Branching example (conditional transitions).
- `burr-mcp serve module:attr` CLI.
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
  `await burr_mcp.spawn_subapp(sub_app, label=...)` to delegate a
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
  - `fn._burr_mcp_validator = callable` on a hand-written Burr action.
  Refusals show up as `error: "validation_failed"` on the wire and
  `refusal_reason: "validation_failed"` in history, with the validator's
  reason and details preserved.

Shipped in v0.8.0:

- `examples/sse_serve.py`: serve over the older SSE transport for
  clients that don't yet support Streamable HTTP.
- Per-action timeout overrides. `ToolSpec(timeout_seconds=N)` in the
  importer applies a timeout to that action only, regardless of the
  server-wide `action_timeout_seconds`. Hand-written Burr actions can
  opt in by setting `fn._burr_mcp_timeout_seconds = N` on the
  decorated function.

Shipped in v0.7.0:

- `burr://trace` resource: read-through of Burr's on-disk
  `LocalTrackingClient` log for the current session's Application.
  Closes the cross-reference gap between burr-mcp's in-memory
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
  everything else burr-mcp provides.
- `ToolSpec` dataclass for the per-tool declarations:
  `reads`/`writes`/`merge_result`/`state_update`/`rename`.
- `examples/import_flat.py` shows the full pattern end-to-end.

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
  affected keys under `_burr_mcp.coerced_keys` so the client knows
  the round-trip is lossy.
- Burr pinned to `>=0.40.2,<0.41` since we rely on internal API
  surface (`Action.fn`, `Action.inputs` tuple shape, `__PRIOR_STEP`).

Next (v1.x):

- Public PyPI release.
- WebSocket transport example.
- A `burr-mcp doctor` CLI subcommand that validates a mount target
  before serving (graph reachability, action signatures, tracker
  configuration).
- Optional Pydantic-model output schemas surfaced as MCP tool
  `outputSchema` for stronger client contracts.

## License

Apache 2.0.
