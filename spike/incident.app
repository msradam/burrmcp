---
# The graph is below in Python. This frontmatter is the part Burr does not have:
# how Theodosia should serve the graph as an MCP server.
name: incident
instructions: >
  Incident-response workflow. Acknowledge, then verify, then resolve;
  conclude only after verify. Read the refusal's valid_next_actions to recover.
session_ttl_seconds: 900
max_sessions: 50
# upstream: tools on other MCP servers the actions may call via call_upstream()
upstream:
  fs:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
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


# Picked up by name and passed to mount(input_validators=...). A callable cannot
# live in YAML, so the MCP-server config that *is* code stays in the body.
input_validators = {}


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
