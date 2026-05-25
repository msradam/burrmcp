# Architecture

How `mount()` turns a Burr `Application` into an MCP server, and the load-bearing
internals behind the four-tool surface.

## The four-tool surface

Every server mounted in STEP mode registers the same tools regardless of how
complex the FSM is:

- `step(action, inputs)` runs one transition.
- `reset_session()` rebuilds this session's Application from the factory.
- `fork_at(sequence_id)` rolls this session back to a prior history entry.
- `fork_from_past(app_id, sequence_id)` resumes another session's state through
  the persister. Hidden from the listing when no `state_loader` or
  `LocalTrackingClient` is wired, since it would only ever refuse.

Plus two synthetic tools from FastMCP's `ResourcesAsTools` transform,
`list_resources()` and `read_resource(uri)`, so tools-only clients can reach the
`burr://` resources without `resources/read`.

The action namespace lives in `step`'s `action` argument schema and at
`burr://graph`, not in the tool listing. The listing stays compact; the agent
learns the verbs from the graph resource.

## The action-selection trick

Burr's `astep` picks the next action via `app.get_next_action()`, which returns
the first transition whose condition is true. But under MCP the *client* named
the action to run, not Burr. `_step_application` monkey-patches
`app.get_next_action` to a lambda returning the client-named action, calls
`astep`, then restores the original. This is the translation between MCP's
"client chose X" semantics and Burr's transition-condition semantics.

Before running, the step checks reachability against the live transitions. An
unreachable action is refused with `invalid_transition` and the response carries
`valid_next_actions`, so a client without its own model of the graph can recover
from a single error.

## Per-session isolation

`mount(...)` accepts either an `Application` instance (shared state across all
sessions) or a callable factory (one Application per MCP session). The session
store is a plain dict keyed by `ctx.session_id`, held in `mount`'s closure
scope. Each entry holds the Application built lazily on first touch, a
per-session `asyncio.Lock`, and the history and subrun records.

Eviction is lazy: stale entries are dropped on the next access, not on a
background timer. `session_ttl_seconds` and `max_sessions` both default to live
values and either can be set to `None` to disable that form of eviction.

FastMCP's `ctx.set_state(serializable=False)` is request-scoped, not
session-scoped, so it is not suitable for caching the Application across calls in
one session. The closure dict is.

## Input coercion middleware

`mount()` builds the FastMCP server with `strict_input_validation=False` and
adds a middleware that, for any `tools/call`, parses JSON-string values into
objects when the tool's declared schema allows object or array. The `step`
tool's `inputs` parameter is typed `dict | str | None` so the advertised schema
includes `string` in its `anyOf`.

Both moves are needed for IBM Bob: it validates outbound requests against the
advertised schema (so the schema must accept the string form) and it serializes
nested-object arguments as JSON strings (so the middleware must coerce them
before the action body runs).

## Stashed serving modes

`TOOLS` (one MCP tool per action, no enforcement) and `DYNAMIC` (per-session
`tools/list_changed` visibility) were carved out into
`src/burrmcp/_experimental/modes.py` once STEP became the sole product. The
`ServingMode` enum keeps `STEP` as its only member. Reviving either mode is a
contained change; see that module's docstring.
