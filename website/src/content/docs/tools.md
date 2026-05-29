---
title: 'MCP tools and resources'
description: 'The fixed tool surface and the theodosia:// resources every mounted server exposes.'
---

Every server mounted in the default mode exposes the same small surface, no
matter how large the underlying state machine is. The action namespace does not
inflate the tool list; it lives in `step`'s `action` argument and at
`theodosia://graph`.

## Tools

| Tool | What it does |
|---|---|
| `step(action, inputs)` | Run one transition. Returns the result, the new state, and `valid_next_actions`, or a structured [refusal](refusals.md). |
| `reset_session()` | Rebuild this session's Application from the factory, discarding state and history. Refuses in shared mode. |
| `fork_at(sequence_id)` | Roll this session back to a prior history entry and continue from there. |
| `fork_from_past(app_id, sequence_id)` | Resume another session's state through the persister. Hidden unless a tracker or `state_loader` is wired. |

Two more tools appear when the client cannot read MCP resources directly
(FastMCP's `ResourcesAsTools` transform): `list_resources()` returns the
`theodosia://` catalog and `read_resource(uri)` returns what a native
`resources/read` would. They route through the same path as the native
resources, so a tools-only client reaches everything below.

## Resources

| URI | Returns |
|---|---|
| `theodosia://graph` | Static FSM topology: actions, transitions, each action's required and optional inputs plus their JSON schemas (`input_schemas`, with full Pydantic `model_json_schema()` for typed inputs), and the state schema (the Pydantic JSON schema when typed state is used). |
| `theodosia://state` | The current state for this session. |
| `theodosia://next` | The actions reachable from the current state. |
| `theodosia://history` | The per-session attempt timeline, including refusals and forks. |
| `theodosia://subruns`, `theodosia://subruns/{id}` | Sub-application index and a sub-run's full timeline. Appears only when the FSM uses `theodosia.spawn_subapp(...)`. |
| `theodosia://trace` | Burr's `LocalTrackingClient` JSONL, mirrored for the agent. |
| `theodosia://session` | Tracker coordinates: project, `app_id`, app directory, partition key. |

## Why the surface is fixed

A constant tool list keeps the agent's choice space small and stable across
turns: the agent always has the same few verbs, and learns the domain actions
from `step`'s schema and `theodosia://graph` rather than from a tool list that
changes shape as state advances. The discipline lives in the graph, not in the
tool catalog. See [Architecture](architecture.md) for how `step` drives Burr
underneath.
