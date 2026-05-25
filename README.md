# Theodosia

[![PyPI](https://img.shields.io/pypi/v/theodosia.svg)](https://pypi.org/project/theodosia/)
[![Python](https://img.shields.io/pypi/pyversions/theodosia.svg)](https://pypi.org/project/theodosia/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-msradam.github.io%2Ftheodosia-31748f.svg)](https://msradam.github.io/theodosia/)
[![Built on Apache Burr](https://img.shields.io/badge/built%20on-Apache%20Burr-31748f.svg)](https://github.com/apache/burr)
[![Built on FastMCP](https://img.shields.io/badge/built%20on-FastMCP-c4a7e7.svg)](https://github.com/jlowin/fastmcp)

AI agents are capable and unpredictable. Given real tools, they skip steps, act out of order, and leave you reconstructing what happened from a chat log. Theodosia puts the agent on rails. You define the workflow once as a [Burr](https://burr.dagworks.io/) state machine, and Theodosia serves it over [MCP](https://modelcontextprotocol.io/) so the agent can only advance one allowed step at a time, with every step recorded.

**The model can be wrong; the model cannot lie about state.**

![demo](demos/demo.gif)

| | |
|---|---|
| **Stays on the rails** | The server enforces the graph. An action that isn't reachable from the current state returns a structured refusal listing the ones that are, and the agent self-corrects from it. |
| **Auditable by default** | Every step, its inputs, the state change, refusals, and timing, recorded to a replayable trace through Burr's tracker and UI. |
| **One portable contract** | Drive the same graph from your own Python (deterministic) or hand it to an external LLM over MCP. The workflow is a versioned artifact, not tied to either. |
| **Built on mature parts** | The workflow engine is Apache Burr; the MCP layer is FastMCP. Theodosia is the thin layer that makes one drive the other. |

## Why this shape works

Current LLM agents are unreliable at procedural work in nameable, structural ways: they skip steps, terminate early or fail to stop, and declare success without verifying. IBM Research's [IT-Bench analysis](https://huggingface.co/blog/ibm-research/itbenchandmast) measured that prompt-level fixes for these failures buy around 15.6%, while a stricter state machine to enforce termination buys up to 53%, and recommends implementing finite state machines outright. Theodosia is that state machine, served to the agent over the wire.

It removes the structural failures: out-of-order steps, skipped gates, premature or missed termination, an unbounded action space. It does not fix reasoning or verification errors inside a valid step, and does not claim to.

Further reading: [IBM IT-Bench + MAST](https://huggingface.co/blog/ibm-research/itbenchandmast) · [Why Do Multi-Agent LLM Systems Fail? (UC Berkeley)](https://arxiv.org/abs/2503.13657) · [Microsoft AIOpsLab](https://www.microsoft.com/en-us/research/blog/aiopslab-building-ai-agents-for-autonomous-clouds/) · [Grafana o11y-bench](https://o11ybench.ai/)

## Install

```bash
uv pip install theodosia     # or: pip install theodosia
```

Python 3.11 through 3.13. Optional extras: `theodosia[observability]`, `theodosia[ui]`, `theodosia[all]`.

## Quickstart

Define a Burr graph, mount it, point an agent at it.

```python
from theodosia import mount

mount(application, name="coffee").run()
```

A client that calls `pay` before `take_order` gets a structured refusal it can recover from:

```json
{ "error": "invalid_transition", "valid_next_actions": ["take_order"] }
```

The valid action set rides on every response, so a client with no model of the graph self-corrects. Full walkthrough and the graph definition: [Quickstart and Architecture](https://msradam.github.io/theodosia/architecture/). Runnable graphs in [`examples/`](examples/).

## What you can build on it

The integration boundary is Burr's `Application`: anything `ApplicationBuilder` supports (typed state, persistence, lifecycle hooks, parallelism, sub-applications, OpenTelemetry) passes through `mount()` with no adapter changes. An action can also reach *other* MCP servers through `call_upstream(...)`, so the graph can drive a filesystem, Kubernetes, or Grafana server while the agent still sees one `step` tool.

Details: [What works through mount()](https://msradam.github.io/theodosia/compatibility/) · [Driving other MCP servers](https://msradam.github.io/theodosia/upstream/)

## Observability

Add a tracker to the builder and every step is recorded to JSONL and replayable in the Burr UI. The agent reads its own trail through `theodosia://` resources; from the terminal the CLI reads the same store (`theodosia sessions show <app-id>`, `theodosia watch`, `theodosia logs --refusals`).

![sessions](demos/observability.gif)

Full surface (resources, CLI, UI, OpenTelemetry): [Observability](https://msradam.github.io/theodosia/observability/).

## CLI

`theodosia serve` / `doctor` / `render` / `sessions` / `watch` / `logs`. `doctor` statically validates a graph and exits nonzero for CI. A downstream package can ship its own command with `build_cli`, baking in its graph. See [CLI](https://msradam.github.io/theodosia/cli/).

## Examples and tests

[`examples/`](examples/) ships self-contained FSMs (pure-FSM, typed state, hooks, persistence, real shellouts, LLM-in-the-graph, SKILL-to-FSM, upstream, multi-graph), each runnable with `uv run python examples/<file>.py`. `uv run pytest` runs the suite (most tests in-process via FastMCP's in-memory client).

## Acknowledgements

Theodosia is glue between two libraries that do the hard parts: [Apache Burr](https://github.com/apache/burr) provides the state-machine `Application`, the transition graph, and the tracking UI; [FastMCP](https://github.com/jlowin/fastmcp) provides the MCP server, the transforms, and the client behind `upstream`. The SKILL demos under `examples/skills/` are reproduced verbatim from Anthropic and Trail of Bits with attribution.

On the name: Theodosia was Aaron Burr's daughter, known for her correspondence with him. The project sits in the same family as Burr and reaches it, which is the role it plays here.

Theodosia is an independent project, not affiliated with or endorsed by the Apache Software Foundation, DAGWorks, the Apache Burr project, or FastMCP. "Apache Burr" and "FastMCP" are referenced only to describe what Theodosia builds on.

## License and notice

Apache 2.0. Theodosia is independent open-source work by Adam Munawar Rahman and does not represent the views of IBM Corporation or any other employer. See [NOTICE.md](NOTICE.md).
