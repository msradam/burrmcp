---
title: 'Personas'
description: 'PERSONA.md identity layer mounted as MCP prompts.'
---

A persona is a markdown file exposing identity and role guidance to the agent
driving a Theodosia FSM. Theodosia loads a directory of `PERSONA.md` files and
exposes each as an MCP prompt the client can fetch at session start. The graph
defines the steps; the persona shapes the trajectory through them.

## File format

The format mirrors SKILL.md from the Agent Skills Open Standard.

```markdown
---
name: on-call-sre
description: Calm on-call SRE; root cause first, blast radius before fix.
voice: terse, direct, no hype       # optional
metadata:
  version: "1.0"
---

# Body markdown

You are an on-call SRE. Investigate before acting. Cite the source
(metric, log, deploy) that supports each claim. The current phase is
{state.phase}; you came in on alert for {state.alert.service}.
```

The frontmatter is parsed as YAML. `name` and `description` are required;
`voice` and `metadata` are free-form. The body is markdown.

## Mounting

Three accepted shapes for the `personas` keyword:

```python
from theodosia import mount

# A directory of PERSONA.md files
mount(build_application, personas="personas/")

# A single file
mount(build_application, personas="on-call-sre.md")

# Inline dict (handy for tests)
mount(build_application, personas={
    "careful": "---\nname: careful\ndescription: c\n---\nbody\n",
})
```

The optional `default_persona="<name>"` keyword names which persona
to surface in the server-level `instructions` for clients that don't pick
one explicitly. Default is the lexically first persona loaded.

## The MCP prompt namespace

Each loaded persona is registered as an MCP prompt named
`theodosia/persona/<name>`. The full namespaced name is what you pass
to `get_prompt`:

```python
from fastmcp import Client

async with Client(server) as c:
    prompts = await c.list_prompts()
    # → [Prompt(name='theodosia/persona/on-call-sre', ...)]
    result = await c.get_prompt("theodosia/persona/on-call-sre")
    print(result.messages[0].content.text)
```

`get_prompt("on-call-sre")` (the bare name) raises
`Unknown prompt: 'on-call-sre'`. Use the namespaced form.

## Frame-aware interpolation

Persona bodies may reference live session state using single-brace
placeholders. Theodosia interpolates them every time the body is rendered:

| Placeholder            | Resolves to                                                    |
|------------------------|----------------------------------------------------------------|
| `{state.<field>}`      | A field of the current session's `Application` state.          |
| `{action.name}`        | The most recently completed action, or the entrypoint name.    |
| `{action.reachable}`   | Comma-separated names of actions reachable next.               |
| `{graph.total_actions}`| The total action count in the graph.                           |
| `{graph.all_actions}`  | Comma-separated names of every action.                         |
| `{session.session_id}` | The MCP session id.                                            |

Placeholder syntax is single-brace `{...}`, not Jinja-style `{{ ... }}`.

Unknown placeholders render as the empty string rather than raising, so a
persona that references a field the FSM hasn't populated yet renders
cleanly until the field exists. Interpolation runs at `get_prompt` time
against whatever state the calling session is in.

Values that resolve to dicts, lists, or tuples are rendered as JSON
(`{"item": "soda"}`), not Python `repr`. Scalars are rendered via `str()`.

## Resources

| URI                                       | Returns                                |
|-------------------------------------------|----------------------------------------|
| `theodosia://personas`                    | Full catalog of loaded personas.       |
| `theodosia://persona`                     | The currently active persona's record. |

## Example

```python
from theodosia import mount, tracker
from burr.core import ApplicationBuilder, State, action

@action(reads=[], writes=["phase", "alert"])
def receive_alert(state: State, service: str) -> State:
    return state.update(phase="triage", alert={"service": service})

def build_application():
    return (
        ApplicationBuilder()
        .with_actions(receive_alert=receive_alert)
        .with_state(phase="idle", alert={})
        .with_entrypoint("receive_alert")
        .with_tracker(tracker(project="incidents"))
        .build()
    )

if __name__ == "__main__":
    mount(
        build_application,
        name="incidents",
        personas="personas/",
        default_persona="on-call-sre",
    ).run()
```

With the `on-call-sre.md` persona above and one completed `receive_alert`
step on `service="checkout"`, `get_prompt("theodosia/persona/on-call-sre")`
returns a body with `{state.phase}` rendered as `triage` and
`{state.alert.service}` as `checkout`.
