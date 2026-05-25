---
title: 'BurrMCP'
description: 'Mount Burr state-machine Applications as MCP servers.'
---

BurrMCP gives an AI agent a stateful, auditable workflow it cannot step outside
of. You define the workflow as a [Burr](https://burr.dagworks.io/) state machine;
BurrMCP serves it over [MCP](https://modelcontextprotocol.io/) so the agent
advances it one transition at a time.

Each Burr `@action` is reachable through one `step(action, inputs)` MCP tool.
State lives on the server. The server enforces transitions: an action that is not
reachable from the current state comes back as a structured refusal listing the
actions that are reachable. Every step is recorded to a replayable trace.

```python
from burrmcp import mount

server = mount(application)
server.run()
```

## Pages

- [Architecture](architecture.md): the four-tool surface, the action-selection
  trick, per-session isolation, input coercion.
- [Observability](observability.md): the `burr://` resources, the terminal CLI,
  the Burr UI, OpenTelemetry.
- [Driving other MCP servers](upstream.md): the `upstream` feature, where a Burr
  action calls tools on other MCP servers.
- [CLI](cli.md): serve, doctor, the observability commands, and shipping your own
  rebranded command with `build_cli`.

The source, examples, and quickstart live in the
[repository](https://github.com/msradam/burrmcp).

BurrMCP is an independent project, not affiliated with the Apache Software
Foundation, DAGWorks, the Apache Burr project, or the FastMCP project.