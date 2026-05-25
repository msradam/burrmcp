# Theodosia

[![PyPI](https://img.shields.io/pypi/v/theodosia.svg)](https://pypi.org/project/theodosia/)
[![Python](https://img.shields.io/pypi/pyversions/theodosia.svg)](https://pypi.org/project/theodosia/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-msradam.github.io%2Ftheodosia-31748f.svg)](https://msradam.github.io/theodosia/)
[![Built on Apache Burr](https://img.shields.io/badge/built%20on-Apache%20Burr-31748f.svg)](https://github.com/apache/burr)
[![Built on FastMCP](https://img.shields.io/badge/built%20on-FastMCP-c4a7e7.svg)](https://github.com/jlowin/fastmcp)

**Theodosia puts an AI agent on rails.** You define a workflow once as a [Burr](https://burr.dagworks.io/) state machine, and Theodosia serves it over [MCP](https://modelcontextprotocol.io/) so the agent can only take the next allowed step, with every step recorded and replayable. The model can be wrong; the model cannot lie about state.

![A real Kimi K2.6 run driven through a gated SRE incident investigation by Theodosia](demos/hero.gif)

*An open 1T-parameter model (Kimi K2.6) investigating a live incident on rails: each Grafana query is recorded as evidence, out-of-budget and out-of-phase calls are refused, and the conclusion stays gated until the evidence cross-references. The investigation FSM ([Phoebe](https://github.com/msradam/phoebe)) is the workflow; Theodosia is what makes the model drive it.*

| What you get | Why it holds |
|---|---|
| **Stays on the rails** | The server enforces the graph. An unreachable action returns a structured refusal listing the ones that are reachable, and the agent self-corrects from it. |
| **Auditable by default** | Every step, its inputs, the state change, refusals, and timing, recorded to a replayable trace through Burr's tracker and UI. |
| **One portable contract** | Drive the same graph from your own Python or hand it to an external LLM over MCP. The workflow is a versioned artifact, not tied to either. |
| **Built on mature parts** | Apache Burr is the workflow engine; FastMCP is the MCP layer. Theodosia is the thin layer that makes one drive the other. |

---

## Why this shape works

Current LLM agents fail at procedural work in nameable, structural ways: they skip steps, terminate early or fail to stop, and declare success without verifying. IBM Research's [IT-Bench analysis](https://huggingface.co/blog/ibm-research/itbenchandmast) measured prompt-level fixes for these at about 15.6%, while a state machine to enforce termination buys up to 53%, and recommends finite state machines outright. Theodosia is that state machine, served over the wire. It removes the structural failures; it does not fix reasoning inside a valid step, and does not claim to.

More: [IBM IT-Bench + MAST](https://huggingface.co/blog/ibm-research/itbenchandmast) · [MAST, UC Berkeley](https://arxiv.org/abs/2503.13657) · [Microsoft AIOpsLab](https://www.microsoft.com/en-us/research/blog/aiopslab-building-ai-agents-for-autonomous-clouds/) · [Grafana o11y-bench](https://o11ybench.ai/)

---

## Install

```bash
uv pip install theodosia     # or: pip install theodosia
```

Python 3.11 through 3.13. Optional extras: `theodosia[observability]`, `theodosia[ui]`, `theodosia[all]`.

---

## Quickstart

```python
from theodosia import mount

mount(application, name="coffee").run()   # `application` is any Burr Application
```

A client that calls `pay` before `take_order` gets a refusal it can recover from, the valid actions ride on every response:

```json
{ "error": "invalid_transition", "valid_next_actions": ["take_order"] }
```

A smaller example, the same mechanism: an agent ordering coffee, refused when it tries to pay before ordering and recovering from the refusal.

![A coffee-order FSM driven over MCP, with a refusal and recovery](demos/demo.gif)

---

## Command line

```bash
theodosia serve module:app      # mount a Burr graph as an MCP server
theodosia render module:app     # draw the state machine in the terminal (--mermaid / --dot)
theodosia doctor module:app     # statically validate the graph; exits nonzero for CI
theodosia sessions show <id>    # full timeline: per-step state diff + timing
theodosia watch                 # live-tail a running session
theodosia logs --refusals       # only the steps that were refused
```

A downstream package can ship its own command (`my-fsm serve`, `my-fsm doctor`, ...) with `build_cli`.

---

## Observability

Every session is recorded through Burr's tracker. Tail a live run, replay a finished one step by step with its state diffs and timing, or open it in the Burr UI for the transition graph and state inspection. Refusals are recorded too: they appear in the timeline like any other step.

![theodosia logs replaying a session timeline, including a refused step](demos/observability.gif)

---

## Documentation

Full docs at **[msradam.github.io/theodosia](https://msradam.github.io/theodosia/)**.

| Section | What it covers |
|---|---|
| [Architecture](https://msradam.github.io/theodosia/architecture/) | The four-tool surface, structured refusals, how `mount()` drives Burr |
| [What works through mount()](https://msradam.github.io/theodosia/compatibility/) | Typed state, persistence, hooks, parallelism, sub-applications, telemetry |
| [Observability](https://msradam.github.io/theodosia/observability/) | The `theodosia://` resources, the CLI, the Burr UI, OpenTelemetry |
| [Driving other MCP servers](https://msradam.github.io/theodosia/upstream/) | `upstream`: a Burr action calling tools on other MCP servers |
| [CLI](https://msradam.github.io/theodosia/cli/) | `serve` / `doctor` / `render` / `sessions` / `watch` / `logs`, and `build_cli` |

---

## Examples and tests

[`examples/`](examples/) ships self-contained FSMs (pure-FSM, typed state, hooks, persistence, real shellouts, LLM-in-the-graph, SKILL-to-FSM, upstream, multi-graph), each runnable with `uv run python examples/<file>.py`. Run the suite with `uv run pytest`.

---

## Acknowledgements

Theodosia is glue between two libraries that do the hard parts: [Apache Burr](https://github.com/apache/burr) provides the state-machine `Application`, the transition graph, and the tracking UI; [FastMCP](https://github.com/jlowin/fastmcp) provides the MCP server, the transforms, and the client behind `upstream`. The SKILL demos under `examples/skills/` are reproduced verbatim from Anthropic and Trail of Bits with attribution.

On the name: Theodosia was Aaron Burr's daughter, known for her correspondence with him. The project sits in the same family as Burr and reaches it, which is the role it plays here.

Theodosia is an independent project, not affiliated with or endorsed by the Apache Software Foundation, DAGWorks, the Apache Burr project, or FastMCP.

## License and notice

Apache 2.0. Theodosia is independent open-source work by Adam Munawar Rahman and does not represent the views of IBM Corporation or any other employer. See [NOTICE.md](NOTICE.md).
