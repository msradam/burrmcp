---
title: Introduction
description: 'Theodosia mounts a Burr Application as an MCP server. Every Burr action is reachable through a single step tool; the server refuses transitions the graph does not allow and records every attempt.'
---

Theodosia mounts a Burr `Application` as an MCP server. Every Burr `@action` is reachable through one `step(action, inputs)` MCP tool. The server checks reachability against the live graph before each action body runs; out-of-order calls return a structured refusal carrying the reachable next actions. Every successful attempt and every refused attempt is written to a per-session log you can replay, fork, or chain-verify.

## Primitives

- `mount(application, ...)` wraps a Burr `Application` (or factory) as a FastMCP server.
- `step(action, inputs)` is the one MCP tool the agent calls. `reset_session`, `fork_at`, and `fork_from_past` round out the four-tool surface.
- A structured refusal has shape `{"error": "<reason>", "valid_next_actions": [...]}`. Five reasons: `invalid_transition`, `unknown_action`, `validation_failed`, `action_timeout`, `action_error`.
- `theodosia://` resources expose graph topology, current state, valid next actions, history, sub-runs, trace, and session identity.
- The session log is hash-chained. `theodosia verify` recomputes the chain and names any edit, reorder, or middle-deletion.

## Try it without an API key

```bash
pip install theodosia
theodosia primer
```

`theodosia primer` walks a bundled coffee-order example in process. No LLM, no network, byte-for-byte the same output every run.

## Build your own

[Build your own agent](tutorial.md) walks writing a workflow, serving it, and driving it with a real agent. [Authoring a graph](authoring.md) is the reference for the Burr building blocks (`@action`, `Condition`, `with_transitions`).

## Going further

- [Refusals](refusals.md): the five refusal shapes and how the agent recovers.
- [Sessions](sessions.md): per-session isolation, `fork_at`, `fork_from_past`, partition keys.
- [Architecture](architecture.md): what `mount()` does and the four MCP tools it registers.
- [Security model](security-model.md): what the ledger does and does not prove.
- [Case study](case-study.md): Kimi K2.6 on Grafana o11y-bench, free-ranging vs gated.
