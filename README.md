# burr-mcp

Mount [Burr](https://burr.dagworks.io/) Applications as
[MCP](https://modelcontextprotocol.io/) servers.

Status: experiment, v0.2.0.

## What this is

A small adapter. You define a state machine with Burr (`@action`,
`ApplicationBuilder`, transitions). `burr_mcp.mount(application)`
returns a FastMCP server that exposes the state machine over the MCP
wire protocol.

Each Burr action becomes an MCP tool. State lives on the server.
Transitions can be enforced by the server, so a client that calls an
action which isn't reachable from current state gets back a structured
error listing the actions that *are* reachable.

`mount(...)` accepts either a built `Application` (shared across all
connected clients) or a callable `() -> Application` (each MCP
session gets its own state). The factory form is the right default
for multi-client servers; the instance form is fine for single-user
local tooling.

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

Every mounted server registers three MCP resources:

| URI | Returns |
|---|---|
| `burr://state` | Current Application state as JSON. Internal Burr keys (`__PRIOR_STEP`, `__SEQUENCE_ID`) filtered. |
| `burr://next` | Action names reachable from the current state. Empty list after a terminal action. |
| `burr://history` | Per-session timeline of every action attempted (successes and refusals). |

A client that wants to know "where am I, what can I do next, and what
already happened in this session" reads those three and has the answer.

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

Refused attempts (invalid transitions, unknown actions) appear in the
same list with `refused: true`, `refusal_reason` set, and
`state_after: null`. Anyone with the history can replay the session
or audit it without filesystem access to Burr's tracker output.

## Install

Currently a private repo. From source:

```bash
git clone git@github.com:msradam/burr-mcp.git
cd burr-mcp
uv sync
```

Python 3.11 through 3.13.

## Run the example

```bash
uv run python examples/coffee_order.py
```

That starts the FastMCP server in stdio mode. Wire it into any MCP
client with a config that points at the command.

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

Tests drive each mode through an in-process FastMCP client. No
subprocess, no stdio framing. Forty-two tests, runs in about a
sixth of a second.

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
store is a plain dict held in `mount`'s closure scope. Entries are
not currently evicted on session end; for long-running servers with
many short sessions this is on the v0.2 list.

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

Next:

- v0.3: session-store eviction on disconnect (long-running servers
  currently leak Application + history entries until restart), HTTP
  and SSE transport examples, optional Burr-tracker passthrough
  exposing on-disk traces via a resource.
- v0.4: subgraph mounting (a Burr subgraph spawned from inside an
  action, exposed as a sub-resource), input validation hooks beyond
  Burr's `inputs` declaration.

## License

Apache 2.0.
