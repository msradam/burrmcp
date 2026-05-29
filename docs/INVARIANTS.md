# Architectural invariants

The load-bearing decisions in Theodosia. If you change one of these,
something else breaks in a way that the test suite will not always
catch. Read this before touching `adapter.py`, the four-tool surface,
or anything in `_step_*`.

This document exists because a 2,300-line `adapter.py` looks scary, and
without these notes a contributor (or a future-self three months from
now) might "refactor" something that is the way it is for a specific,
load-bearing reason.

## 1. The four-tool surface is permanent

`step`, `reset_session`, `fork_at`, `fork_from_past`. New capability
goes into one of:

- `step`'s `inputs` argument schema (the action namespace lives here, as
  data, not as a tool listing)
- A new `theodosia://...` resource
- The structured response payload (a new field on the success or
  refusal shape)

**Never** add a new top-level MCP tool. The constant-size tool listing
is the architectural property — every Theodosia-served FSM presents the
same surface to the agent regardless of graph complexity, so the agent
learns the verbs from the graph resource, not from the tool listing.

Tests that pin this: `tests/test_step_mode.py`,
`tests/test_tool_annotations.py`. A future
`tests/test_adapter_contract.py` (Hypothesis property) is on the
backlog.

## 2. `_step_application` monkey-patches `app.get_next_action`

In `src/theodosia/adapter.py`, around the body of `_step_application`:
the MCP semantic is "client chose action X." The Burr semantic is "I
will pick the next action by evaluating transition conditions and
running the first true one." These do not match. We bridge by setting
`app.get_next_action = lambda: <named-action>` for one call, then
restoring the original.

**Do not** refactor this into "just call the action directly" or "use a
state machine library that lets you specify the next action." Burr's
runtime needs `astep` to drive the action so that the lifecycle hooks
fire, the tracker logs, the persister snapshots. The monkey-patch is
the smallest correct bridge.

If Burr ever exposes a public `astep_with_action(name=...)` API, swap
to that and delete the monkey-patch. Until then, it stays.

## 3. Session isolation is `factory()` per `ctx.session_id`

`_SessionStore` is a plain dict keyed by `ctx.session_id` (FastMCP's
per-session identifier), held in `mount()`'s closure scope. Factory
mode is the default; passing an `Application` instance directly is
legacy (shared state across sessions, harder to reason about, breaks
under any kind of concurrent load).

The store is closure-held, not module-global, so multiple mounted
servers in one process do not bleed into each other's sessions.
Eviction is lazy (on-access, no background timer) — `session_ttl_seconds`
and `max_sessions` are the two knobs.

If you ever need to share state across sessions, do it through a
persister or an external store, not by reaching for shared-app mode.

## 4. Async action bodies are required with a persister

Burr's `astep` delegates to sync `_step` for sync action bodies and
fires `post_run_step` with **stale (pre-action) state**. That means
persister snapshots and tracker `end_entry` rows record the wrong state
for sync-bodied actions. The CLI works around this for non-terminal
rows by forward-scanning, but the terminal row's state stays stale and
any session resumed via `fork_from_past` resumes from the wrong
snapshot.

When the FSM uses `with_state_persister(...)`, **declare action bodies
`async def`**. `doctor` warns on this since commit `342ce74`; the warn
fires for any `with_state_persister` + sync action combination. If
you ship a new persister demo, the action bodies must be async.

If a Burr release ever fixes the `astep` + sync action ordering, the
warning becomes obsolete and we can drop it. Until then, the warning
stays.

## 5. FastMCP wraps `sys.stderr`; upstream needs `sys.__stderr__`

FastMCP wraps `sys.stderr` in a `StringIO` for protocol cleanliness
inside a running server. The upstream stdio client opener
(`mcp.client.stdio`) calls `.fileno()` on whatever stderr it inherits.
A `StringIO` has no `.fileno()`, so the subprocess crashes with
`io.UnsupportedOperation: fileno`.

`theodosia.upstream._as_transport` therefore defaults
`log_file=sys.__stderr__` on the `StdioTransport` it builds.
`sys.__stderr__` is the original interpreter-level stderr and has a
real `.fileno()` regardless of what's been done to `sys.stderr`. Users
can override per-config with `{"log_file": Path(...)}` or any `TextIO`.

This bug ate a 0.3 release. The fix is in commit `e48d4d8`; the test
that pins it is `tests/test_upstream_stderr_default.py`. **Do not**
remove the default thinking it's redundant — the default IS the fix.

## 6. Bob's JSON-string args need both a wider schema AND middleware

IBM Bob (as of mid-2026) serialises nested-object tool arguments as
JSON strings rather than as nested objects: `"inputs": "{\"item\":
\"mocha\"}"` instead of `"inputs": {"item": "mocha"}`. FastMCP's
input-schema validator then rejects with `params/inputs must be
object`.

The fix has two parts and **both are needed**:

1. The `step` tool's `inputs` parameter is typed `dict | str | None` so
   the advertised JSON Schema's `anyOf` includes `string`. Bob
   validates outbound requests against the advertised schema, so a
   server-side coercion middleware alone is not enough — the schema
   must accept the string form.
2. `_build_coercion_middleware` (now in `theodosia/_coercion.py`)
   intercepts `tools/call` and re-parses any string value whose
   declared schema accepts object or array. Clients that send arbitrary
   strings need the middleware to coerce them before the action body
   runs.

Either piece on its own does not work. The test that pins this is
`tests/test_input_coercion.py`.

## 7. `fork_from_past` binds `partition_key` to the session

`fork_from_past(app_id, partition_key=...)` could let an agent for
tenant A load tenant B's persisted state by passing tenant B's
partition. The fix (commit `4ea1168`) reads the session's bound
partition (set by the factory's `with_identifiers(partition_key=...)`)
and refuses the call when the caller's value disagrees. The refusal
fires **before** the persister is reached, so a malicious tenant cannot
even confirm whether the requested app_id exists in another partition.

The test that pins this is
`tests/test_fork_from_past_partition_binding.py`. **Do not** "let the
caller specify any partition" thinking it's a flexibility win — it's a
cross-tenant leak.

---

## When to read this

- Before touching `_step_application` or `_step_streaming_action`
- Before adding a new MCP tool (you almost certainly want a resource or
  a `step`-args extension instead)
- Before changing how `partition_key`, `app_id`, or the persister flow
  through `mount()`
- Before responding to an issue or PR that proposes refactoring any of
  the above

## When to update this

- When an upstream library (Burr, FastMCP, MCP itself) changes
  semantics such that an invariant becomes obsolete or shifts
- When a new bug-finding dogfood run surfaces a load-bearing decision
  that wasn't documented
- When you find yourself writing the same "do not refactor this, here's
  why" comment in two places — promote it here
