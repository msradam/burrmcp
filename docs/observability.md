# Observability

Every narrative demo wires `LocalTrackingClient(project="<demo>-demo")`, so each
MCP session writes a JSONL log under `~/.burr`. Three surfaces read it: the
`burr://` MCP resources (for the agent), the `burrmcp` CLI (for the terminal),
and the Burr web UI (for replay).

## For the agent: `burr://` resources

| URI | Returns |
|---|---|
| `burr://graph` | Static FSM topology (actions, transitions, state schema). |
| `burr://state` | Current state for this session. |
| `burr://next` | Valid next actions from the current state. |
| `burr://history` | Per-session attempt timeline, including refusals. |
| `burr://subruns`, `burr://subruns/{id}` | Sub-app index and full timeline. |
| `burr://trace` | Burr's LocalTrackingClient JSONL mirrored for the agent. |
| `burr://session` | Tracker coordinates: project, app_id, app_dir, partition_key. |

`burr://history` captures what the *agent* attempted (including refused steps);
`burr://trace` captures what *Burr* executed. A refused attempt carries one of
five `refusal_reason` values (`invalid_transition`, `unknown_action`,
`action_error`, `action_timeout`, `validation_failed`) so the agent can tell "the
FSM said no" from "the action's code raised."

## For the terminal: the CLI

```bash
burrmcp sessions ls                 # recent sessions, most recent first
burrmcp sessions show <app-id>      # full timeline: per-step state diff + timing
burrmcp sessions tail [app-id]      # live-tail a running session
burrmcp watch [app-id]              # alias for `sessions tail`
burrmcp logs [app-id]               # compact one-line-per-step, greppable
burrmcp logs --refusals --plain     # only steps that errored, pipe-friendly
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
burrmcp ui
```

Opens Burr's web UI, which visualizes every state transition for any tracker
project on disk: state diffing, graph view, replay. Bootstraps via `uvx` on first
run; permanent install with `uv pip install 'burrmcp[ui]'`.

## OpenTelemetry and custom sinks

For OTel spans, install `burrmcp[observability]` and use Burr's
`OpenTelemetryBridge` as a lifecycle adapter (`examples/with_otel.py`). Custom
span sinks (Datadog, Honeycomb, in-memory) work through Burr's `PreStartSpanHook`
/ `PostEndSpanHook` / `DoLogAttributeHook` (`examples/custom_telemetry.py`).
