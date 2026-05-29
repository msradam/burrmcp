---
title: 'What works through mount()'
description: 'Burr surfaces that pass through the adapter.'
---

The integration boundary is Burr's `Application`. Anything supported by
`ApplicationBuilder` passes through `mount()` without adapter changes, including
parallelism, persistence, telemetry, and library coexistence. Anything missing
from this table has not been exercised yet.

| Burr surface | Through `mount()` | Demo / evidence |
|---|---|---|
| `@action`, `with_transitions`, `with_state`, `with_entrypoint` | Yes (core path) | every demo |
| `Condition.expr` / `.when` / `.default` | Yes | `coffee_order`, `chargen`, `incident_response` |
| `with_tracker(LocalTrackingClient)` | Yes; surfaced at `theodosia://trace` | every narrative demo |
| `with_state_persister(BaseStatePersister)` | Yes | `sqlite_persister` |
| `with_typed_state(Pydantic)` | Yes; JSON schema exported via `theodosia://graph` `state_schema` | `typed_state_loan` |
| `@pydantic_action` decorator | Yes; subset-model machinery surfaces the action's typed slice | `pydantic_actions` |
| `with_identifiers(partition_key=...)` (multi-tenancy) | Yes; surfaces in `theodosia://session.partition_key` | `partition_key_tenants` |
| `with_parallel_executor(...)` | Yes (default thread-pool); `RayExecutor` swap documented inline | `burr_map_parallel` |
| `MapStates` / parallel sub-runs | Yes | `burr_map_parallel` |
| Streaming actions | Yes; emitted as MCP progress notifications | `streaming_narrate` |
| Async actions (`async def @action`) | Yes | `parallel_research`, `mellea_qiskit_migration` |
| Sub-Application composition | Yes; `theodosia://subruns` indexes `spawn_subapp` calls | `incident_response`, `subgraphs` |
| OpenTelemetry (`OpenTelemetryBridge`) | Yes | `with_otel` |
| User-defined lifecycle hooks (`PreRunStepHook` / `PostRunStepHook` / etc.) | Yes; attach via `mount(..., hooks=[hook1, hook2])` or via `ApplicationBuilder.with_hooks(...)` in your factory | `pipeline_hooks` |
| Async hooks + envelope hooks (`PreRunStepHookAsync`, `PostApplicationCreateHook`, `PreRunExecuteCallHookAsync`, etc.) | Yes; `await`ed around each action; envelope hooks wrap every execute boundary including MCP `step` | `async_hooks` |
| `@streaming_action.pydantic` + streaming hooks (`PreStartStreamHook`, `PostStreamItemHook`, `PostEndStreamHook`) | Yes; chunks typed by `stream_type`, hooks fire when streaming actions are driven via MCP `step` (adapter uses `app.astream_result`) | `streaming_hooks` |
| Span tracing hooks (`PreStartSpanHook`, `PostEndSpanHook`, `DoLogAttributeHook`) via the `__tracer` parameter | Yes; user-defined hook captures sub-span trees and attribute logs alongside `OpenTelemetryBridge` | `custom_telemetry`, `with_otel` |
| `ApplicationBuilder.initialize_from(persister, fork_from_app_id=..., fork_from_sequence_id=...)` (builder-level state forking) | Yes; two Applications share an initial state via a persister, then walk independently with their own `uid`s | `state_forking`, `sqlite_persister` |
| `AsyncBaseStatePersister` + `PersisterHookAsync` | Yes; `await persister.save(...)` runs inline on the MCP step path (adapter drives `astep`, hooks fire async) | `async_persister` |
| `@trace` decorator (auto-span any function called from an action) | Yes; nested call graph maps onto the span tree, inputs/outputs auto-logged as attributes | `trace_decorator` |
| Burr's prebuilt `StateAndResultsFullLogger` (zero-config JSONL audit log) | Yes; one JSONL row per action with post-step state + result + timing | `full_logger` |
| FastMCP `ctx.sample` from inside an action body | Yes; `theodosia.current_mcp_context()` returns the FastMCP `Context` so actions can delegate LLM work to the connected agent's model | `caller_sample` |
| FastMCP `ctx.elicit` from inside an action body | Yes; action bodies can pop interactive user confirmation prompts mid-step for safety-rail gates | `elicit_confirm` |
| Output schema on the `step` tool | Yes; clients see a typed response contract (discriminator `error` + per-shape fields) in the MCP tool listing | always-on |
| FastMCP middleware (timing, structured logging, rate limiting, custom) | Yes; attach via `mount(..., middleware=[mw1, mw2])` or via `server.add_middleware(...)` after `mount()` returns | `with_middleware` |
| `with_graph(Graph)` / `with_graphs(...)` (reusable graph fragments) | Yes; same `Graph` object embedded in multiple Applications | `subgraph_composition` |
| Class-based `Action` subclasses (escape from `@action`) | Yes; one class, configured instances | `class_action` |
| Hamilton driver inside an action body | Yes (no special integration) | `hamilton_features` |
| `app.run(halt_after=...)` auto-routing | Burr-level only | MCP path always uses agent-chosen actions via `step` |

## Experimental: lifting a flat FastMCP server

:::caution
Experimental, not yet validated end to end through a live agent. The unit tests
in `tests/test_importing.py` exercise the mechanism, but it has not been driven
through a real MCP client the way the `step` surface has. Build a Burr graph
directly for anything you depend on.
:::

`burr_app_from_fastmcp(...)` takes a flat FastMCP server and lifts its tools into
a Burr `Application`: you supply the transitions and a `ToolSpec` per tool
declaring what state it reads and writes, and the lifted tools then gain the
transition enforcement and the audit trail. It is intended as a migration path
for an existing flat server. Until it is validated through an agent, treat
building a graph directly as the supported path.