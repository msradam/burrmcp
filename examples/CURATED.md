# Curated examples

Six demos that cover Theodosia's breadth without overlap. Read them in
order if you are new; jump straight to the one that matches your use
case if you are not.

Each section names: what the demo is, what Theodosia primitive it
exercises, and the one-liner to run it.

## 1. `coffee_order.py` — the canonical FSM

Five actions, non-linear graph with an `add_modifier` loop and a
`cancel` escape. No external dependencies. The simplest interesting
shape: gated transitions, refusals, terminal states.

Run it:

```bash
theodosia serve coffee_order:build_application --app-dir examples
```

What to look for: connect a client and call `step(action="cancel")`
from the wrong state. The refusal carries the list of actions that
were reachable, so a smart client can self-correct.

The load-bearing primitive in this file is `Condition.expr("stage ==
'ordered'")` on the transitions. That string is evaluated against the
live state at every step; the agent's `step(action=X)` is allowed
only when the condition is true. That is how the rails are gated.

## 2. `incident_response.py` — real ops on shipped data

SRE workflow that parses real Alertmanager JSON, slices a shipped
service log, correlates with a deploy timeline, runs a remediation
loop, and verifies. Uses `spawn_subapp` for the investigation
sub-graph. This is the demo recorded in the README's GIF.

Run it:

```bash
theodosia serve incident_response:build_application --app-dir examples
```

Exercises: structured refusals as procedural gates, sub-application
composition, hub topology, real-world inputs.

## 3. `differential_review.py` — SKILL converted to FSM

Trail of Bits' differential-review SKILL recast as a 7-phase FSM with
a HIGH-risk gate that enforces adversarial + deep-context phases
structurally. Demonstrates the "convert expert knowledge into FSM
guardrails" pattern.

Run it:

```bash
theodosia serve differential_review:build_application --app-dir examples
```

Exercises: phase-as-state, conditional risk gating, no server-side
LLM (caller-LLM-driven).

## 4. `granite_oncall.py` — LLM in the graph

Granite-via-Ollama alert classifier with retry-as-transitions for
malformed output (max 3 attempts before `route_to_human`). Local
inference, full audit trail through Burr's tracker.

Prerequisite:

```bash
ollama pull granite4:micro
```

Run it:

```bash
theodosia serve granite_oncall:build_application --app-dir examples
```

Exercises: server-side LLM call inside an action body, malformed-output
recovery, terminal escape.

## 5. `sqlite_persister.py` — durable state

Custom `BaseStatePersister` over SQLite. Shows the persister contract
end to end: save, load, `fork_from_past` to resume a session by id.

Run it:

```bash
theodosia serve sqlite_persister:build_application --app-dir examples
```

Exercises: state durability, fork semantics, the persister contract.

## 6. `multi_graph.py` — multiple FSMs in one server

Two graphs (orders + tickets) mounted side by side via `mount_multi`.
Tools land at `orders_step` and `tickets_step`; resources at
`theodosia://orders/graph` and `theodosia://tickets/graph`. The
parent `theodosia://apps` index lists what is mounted.

Run it (the demo composes the parent FastMCP server itself and is run
directly, not through `theodosia serve`):

```bash
python examples/multi_graph.py
```

Exercises: namespaced multi-app composition, the only demo using
`mount_multi`.

## After the curated six

Promote next as needed:

- `adaptive_crag.py` — self-correcting RAG with LLM grading loop.
- `parallel_research.py` — concurrent sub-applications via
  `asyncio.gather`.
- `elicit_confirm.py` — human-in-the-loop confirmation gate.
- `pipeline_hooks.py` — `Pre/PostRunStepHook` via `with_hooks`.
- `with_otel.py` — OpenTelemetry bridge end to end.

The full catalog with one-line summaries is in `README.md`.
