---
title: 'Authoring a graph'
description: 'Build a Burr Application from scratch and serve it with Theodosia.'
---

This is the part Theodosia does not do for you: writing the Burr state machine.
If you have never used Burr, this page is a complete, runnable starting point,
plus the two traps newcomers hit.

## A minimal served graph

A workflow is actions plus the transitions between them. Each `@action` declares
what state it reads and writes. Transitions wire actions together, optionally
behind a condition. Theodosia mounts the built `Application` as an MCP server.

```python
# incident.py
from burr.core import ApplicationBuilder, Condition, State, action
from theodosia import mount, tracker


@action(reads=[], writes=["acked"])
def acknowledge(state: State) -> State:
    return state.update(acked=True)


@action(reads=[], writes=["verified"])
def verify(state: State) -> State:
    # real work goes here; this just marks the gate satisfied
    return state.update(verified=True)


@action(reads=["verified"], writes=["resolution"])
def resolve(state: State) -> State:
    return state.update(resolution="closed")


@action(reads=[], writes=["resolution"])
def escalate(state: State) -> State:
    return state.update(resolution="escalated to owner")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(acknowledge=acknowledge, verify=verify, resolve=resolve, escalate=escalate)
        .with_transitions(
            ("acknowledge", "verify"),
            # resolve is gated: only reachable once verify set verified=True
            ("verify", "resolve", Condition.expr("verified == True")),
            # escalate is an unconditional escape from verify (see the trap below)
            ("verify", "escalate", Condition.expr("True")),
        )
        .with_tracker(tracker(project="incident"))
        .with_entrypoint("acknowledge")
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="incident").run()
```

Pass the factory function (`build_application`) to `mount`, not a built
`Application`, so each MCP session gets its own isolated state. Serve it
directly (`python incident.py`) or through the CLI:

```bash
theodosia serve incident:build_application --name incident
theodosia doctor incident:build_application   # validate before serving
```

`resolve` cannot be called until `verify` has run and set `verified=True`. That
is the whole point: the gate lives in the graph, not in a prompt.

## Trap 1: two unconditional exits from one action

Burr allows only one *default* (conditionless) transition per source action. If
a runbook needs more than one unconditional exit from the same step, for example
both `resolve` and `escalate` reachable from `verify`, a second bare transition
fails:

```text
ValueError: Transition `verify` -> `escalate` is redundant --
a default transition has already been set for `verify`
```

Give each such edge an explicit always-true condition instead of relying on the
default. `Condition.expr("True")` works, as shown above; an equivalent is a
named condition:

```python
ALWAYS = Condition(keys=[], resolver=lambda _state: True, name="always")
.with_transitions(
    ("verify", "resolve", Condition.expr("verified == True")),
    ("verify", "escalate", ALWAYS),
)
```

Theodosia returns every transition whose condition evaluates true as a valid
next action, so multiple true conditions from one source all show up in
`valid_next_actions`. The agent (or your code) then picks one.

## Trap 2: where your sessions are written

Theodosia's `tracker(project=...)` writes to `~/.theodosia`, which is also where
the observability CLI looks by default, so `theodosia sessions ls -p incident`
finds your runs with no extra flags. If you instead wire Burr's native tracker
(`with_tracker("local", project=...)`), it writes to `~/.burr`, and you must
point the CLI at it: `theodosia sessions ls --home ~/.burr -p incident`. Pick
one and stay consistent.

## Next

- [Architecture](architecture.md): the four-tool surface and how `step` drives Burr.
- [Observability](observability.md): tail, replay, and the Burr UI for any served graph.
- [CLI](cli.md): `serve`, `doctor`, and the observability commands.
