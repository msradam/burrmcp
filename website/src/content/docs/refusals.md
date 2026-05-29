---
title: 'Refusals and recovery'
description: 'The structured refusal shapes the step tool returns, and how an agent recovers from them.'
---

When a `step` cannot proceed, Theodosia does not throw an opaque error or let
the action half-run. It returns a structured refusal with a stable shape, and
every refusal carries `valid_next_actions`, the list of actions reachable from
the current state. An agent with no model of the graph can read that list and
correct itself in one turn.

There are five refusal shapes, distinguished by the `error` field.

## `invalid_transition`

The action exists but is not reachable from the current state. The graph
blocked it before the action body ran, so no state changed.

```json
{
  "error": "invalid_transition",
  "requested": "pay",
  "valid_next_actions": ["take_order"],
  "message": "action 'pay' is not reachable from current state. Valid actions now: ['take_order']."
}
```

This is the load-bearing refusal: it is how the state machine enforces order.
The agent retries with one of `valid_next_actions`.

## `unknown_action`

The requested action is not in the FSM at all (a typo or a hallucinated verb),
as opposed to `invalid_transition`, where the action exists but is not reachable
yet. The response carries `known_actions`, every action name in the graph.

```json
{
  "error": "unknown_action",
  "requested": "tako_order",
  "known_actions": ["take_order", "add_modifier", "pay", "fulfill", "cancel"]
}
```

## `validation_failed`

The action is reachable, but an input validator rejected the inputs before the
body ran. State is unchanged.

```json
{
  "error": "validation_failed",
  "requested": "add_modifier",
  "reason": "modifier must be one of: oat, soy, almond",
  "details": { "field": "modifier", "got": "moon" },
  "valid_next_actions": ["add_modifier", "pay", "cancel"]
}
```

Validators are wired through `mount(..., input_validators={...})`.

## `action_timeout`

The action was reachable and ran, but exceeded `action_timeout_seconds`
(configured on `mount`). It surfaces as a refusal rather than a hang.

The timeout fires for both async and sync action bodies. Sync bodies are
detected and run in a worker thread so a blocking call (`time.sleep`, a
blocking HTTP request, a tight CPU loop) cannot freeze the event loop and
defeat the timer. The orphaned thread keeps running until the body
returns; Python cannot safely kill threads, so the client gets the
structured refusal while the body completes in the background.

```json
{
  "error": "action_timeout",
  "requested": "fetch_report",
  "timeout_seconds": 30,
  "message": "action 'fetch_report' exceeded its 30s timeout.",
  "valid_next_actions": ["fetch_report", "cancel"]
}
```

## `action_error`

The action was reachable and ran, but its body raised. The exception type and
message are passed through so the agent can react to the actual failure (a bad
file path, a failed precondition, an upstream error).

```json
{
  "error": "action_error",
  "requested": "edit_file",
  "error_type": "ValueError",
  "error_message": "must read the file before editing it",
  "valid_next_actions": ["read_file", "edit_file"]
}
```

This is the second gate described in [Authoring](authoring.md): the graph
refuses out-of-order calls with `invalid_transition`; an action body raising
`ValueError` for a finer precondition surfaces here. Both are recoverable.

## The recovery contract

Every response, success or refusal, carries `valid_next_actions`. A successful
step also returns the action's result and the new state. So the agent's loop is
the same whether the last step succeeded or was refused: read
`valid_next_actions`, pick one, send the next `step`. The current valid actions
are also always available out of band at the [`theodosia://next`](tools.md)
resource and the full attempt timeline, including refusals, at
`theodosia://history`.
