---
name: incident
description: A tiny incident-response FSM in one file, served by Theodosia.
---
from burr.core import ApplicationBuilder, Condition, action


@action(reads=[], writes=["acked"])
def acknowledge(state):
    return state.update(acked=True)


@action(reads=[], writes=["verified"])
def verify(state):
    return state.update(verified=True)


@action(reads=["verified"], writes=["resolution"])
def resolve(state):
    return state.update(resolution="closed")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(acknowledge=acknowledge, verify=verify, resolve=resolve)
        .with_transitions(
            ("acknowledge", "verify"),
            ("verify", "resolve", Condition.expr("verified == True")),
        )
        .with_state(acked=False, verified=False, resolution=None)
        .with_entrypoint("acknowledge")
        .build()
    )
