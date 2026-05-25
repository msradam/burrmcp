---
title: 'Observability'
description: 'The theodosia:// resources, the terminal CLI, the Burr UI, OpenTelemetry.'
---

Every narrative demo wires `LocalTrackingClient(project="<demo>-demo")`, so each
MCP session writes a JSONL log under `~/.burr`. Three surfaces read it: the
`theodosia://` MCP resources (for the agent), the `theodosia` CLI (for the terminal),
and the Burr web UI (for replay).

![theodosia logs replaying a session timeline, including a refused step](/theodosia/observability.gif)

## For the agent: `theodosia://` resources

| URI | Returns |
|---|---|
| `theodosia://graph` | Static FSM topology (actions, transitions, state schema). |
| `theodosia://state` | Current state for this session. |
| `theodosia://next` | Valid next actions from the current state. |
| `theodosia://history` | Per-session attempt timeline, including refusals. |
| `theodosia://subruns`, `theodosia://subruns/{id}` | Sub-app index and full timeline. |
| `theodosia://trace` | Burr's LocalTrackingClient JSONL mirrored for the agent. |
| `theodosia://session` | Tracker coordinates: project, app_id, app_dir, partition_key. |

`theodosia://history` captures what the *agent* attempted (including refused steps);
`theodosia://trace` captures what *Burr* executed. A refused attempt carries one of
five `refusal_reason` values (`invalid_transition`, `unknown_action`,
`action_error`, `action_timeout`, `validation_failed`) so the agent can tell "the
FSM said no" from "the action's code raised."

## For the terminal: the CLI

```bash
theodosia sessions ls                 # recent sessions, most recent first
theodosia sessions show <app-id>      # full timeline: per-step state diff + timing
theodosia sessions tail [app-id]      # live-tail a running session
theodosia watch [app-id]              # alias for `sessions tail`
theodosia logs [app-id]               # compact one-line-per-step, greppable
theodosia logs --refusals --plain     # only steps that errored, pipe-friendly
```

`app-id` defaults to the most-recently-touched session and accepts a uuid prefix.
`show` and `watch` render a table with a per-step state diff, latency, and a
status glyph (a refused step shows red with its error message). `logs --plain`
drops color and glyphs for `grep`. Add `--json` to `ls` and `show` for machine
output.

The CLI reads the on-disk JSONL directly, so it can inspect a session running
right now in another process without opening the web UI.

## For replay: the Burr UI

```bash
theodosia ui
```

Opens Burr's web UI, which visualizes every state transition for any tracker
project on disk: state diffing, graph view, replay. Bootstraps via `uvx` on first
run; permanent install with `uv pip install 'theodosia[ui]'`.

## OpenTelemetry and custom sinks

For OTel spans, install `theodosia[observability]` and use Burr's
`OpenTelemetryBridge` as a lifecycle adapter (`examples/with_otel.py`). Custom
span sinks (Datadog, Honeycomb, in-memory) work through Burr's `PreStartSpanHook`
/ `PostEndSpanHook` / `DoLogAttributeHook` (`examples/custom_telemetry.py`).