# Theodosia

[![PyPI](https://img.shields.io/pypi/v/theodosia?style=flat-square&color=573e8a&logo=pypi&logoColor=white)](https://pypi.org/project/theodosia/)
[![tests](https://img.shields.io/github/actions/workflow/status/msradam/theodosia/ci.yml?branch=main&style=flat-square&label=tests)](https://github.com/msradam/theodosia/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-573e8a?style=flat-square)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-theodosia-573e8a?style=flat-square&logo=astro&logoColor=white)](https://msradam.github.io/theodosia/)
[![Built on Apache Burr](https://img.shields.io/badge/built%20on-Apache%20Burr-c4a7e7?style=flat-square)](https://github.com/apache/burr)
[![Built on FastMCP](https://img.shields.io/badge/built%20on-FastMCP-c4a7e7?style=flat-square)](https://github.com/jlowin/fastmcp)

**Theodosia puts an AI agent on rails.** You define a workflow once as a [Burr](https://burr.dagworks.io/) state machine, and Theodosia serves it over [MCP](https://modelcontextprotocol.io/) so the agent can only take the next allowed step, with every step recorded and replayable.

> **The model can be wrong; the model cannot lie about state.**

![A real Kimi K2.6 run driven through a gated SRE incident investigation by Theodosia](demos/hero.gif)

*An open 1T-parameter model (Kimi K2.6) investigating a live incident on rails: each Grafana query is recorded as evidence, out-of-phase calls are refused, and the conclusion stays gated until the evidence cross-references. The investigation FSM ([Phoebe](https://github.com/msradam/phoebe)) is the workflow; Theodosia is what makes the model drive it.*

| What you get | Why it holds |
|---|---|
| **Stays on the rails** | The server enforces the graph. An unreachable action returns a structured refusal listing the ones that are reachable, and the agent self-corrects from it. |
| **Auditable and replayable** | Every step, its inputs, the state change, refusals, and timing are recorded. Replay any session step by step (`theodosia sessions show`, the Burr UI) and fork from any past state. |
| **One portable contract** | Drive the same graph from your own Python or hand it to an external LLM over MCP. The workflow is a versioned artifact, not tied to either. |
| **Built on mature parts** | Apache Burr is the workflow engine; FastMCP is the MCP layer. Theodosia is the thin layer that makes one drive the other. |

---

## Why this shape works

LLM agents fail at procedural work in predictable ways: they skip steps, stop too early or not at all, and declare success without verifying. Research on why multi-agent systems fail ([MAST](https://arxiv.org/abs/2503.13657), Cemri et al.) finds the interventions that held came from architecture, external verification and a termination-enforcing state machine, not prompt tweaks. Theodosia is that state machine, served over the wire. It removes the structural failures, not reasoning errors inside a valid step: the agent keeps its full toolset (including other MCP servers via `upstream`) and chooses freely within each step.

More: [IBM IT-Bench + MAST](https://huggingface.co/blog/ibm-research/itbenchandmast) · [MAST, UC Berkeley](https://arxiv.org/abs/2503.13657) · [Microsoft AIOpsLab](https://www.microsoft.com/en-us/research/blog/aiopslab-building-ai-agents-for-autonomous-clouds/) · [Grafana o11y-bench](https://o11ybench.ai/)

### What the rails do, shown

Two grader-verified cases ([case study](https://msradam.github.io/theodosia/case-study/)):
the same model (Kimi K2.6) on [o11y-bench](https://o11ybench.ai/) incident tasks,
run free-ranging with the raw Grafana toolset versus on rails through
[Phoebe](https://github.com/msradam/phoebe). Free-ranging, it trails off without
an answer, on one task across all three runs, on another it solves it twice and
abandons it once. On rails, the `conclude` gate forces a committed, correct
conclusion every time. o11y-bench's own grader is the witness: *"There is no
final response message in the transcript, it ends with tool calls and thinking
blocks."*

On these tasks the rails did not cost accuracy; what they added is that the agent
finished the ones it would otherwise abandon, and that every run is a recorded,
replayable, forkable artifact (every step, input, state change, and refusal) that
a free-ranging agent at the same accuracy cannot hand you. A full aggregate
across the category is pending a clean benchmark run; the design rationale,
including what rails do not fix, is in the
[research foundation](https://msradam.github.io/theodosia/research-foundation/).

---

## Try it in 30 seconds

```bash
uv pip install theodosia
theodosia primer
```

No API key, no LLM, no setup. Walks the coffee-order FSM through Theodosia's `step` tool in-process, prints the timeline with state diffs, then provokes one structured refusal so you see what an agent recovers from. Same output every run.

## Install

```bash
uv pip install theodosia     # or: pip install theodosia
```

Python 3.11 through 3.13. Optional extras: `theodosia[observability]`, `theodosia[ui]`, `theodosia[all]`.

On a slim Docker image (`python:3.13-slim`, Alpine) the install pulls a `psutil` build that needs `gcc` and `python3-dev`. Either use the full `python:3.13` image, or `apt-get install -y gcc python3-dev` before `pip install`.

---

## Quickstart

```python
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from theodosia import mount


@action(reads=[], writes=["item", "stage"])
def take_order(state: State, item: str) -> State:
    return state.update(item=item, stage="ordered")


@action(reads=["stage"], writes=["stage"])
def pay(state: State, amount: float) -> State:
    return state.update(stage="paid")


def build_application():
    is_ordered = Condition.expr("stage == 'ordered'")
    return (
        ApplicationBuilder()
        .with_actions(take_order=take_order, pay=pay)
        .with_transitions(("take_order", "pay", is_ordered))
        .with_state(item=None, stage="empty")
        .with_entrypoint("take_order")
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="coffee").run()
```

Save as `coffee.py` and run `python coffee.py` to serve over stdio. Pass a factory (a callable returning a built `Application`) so each MCP session gets its own isolated state. Passing an already-built `Application` works too but shares state across sessions.

To exercise it without a real client, use FastMCP's in-process `Client`:

```python
import asyncio
from fastmcp import Client
from coffee import build_application

async def main():
    async with Client(mount(build_application, name="coffee")) as client:
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        print(r.structured_content)   # refusal: take_order required first
        r = await client.call_tool("step", {"action": "take_order", "inputs": {"item": "mocha"}})
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        print(r.structured_content)

asyncio.run(main())
```

A client that calls `pay` before `take_order` gets a refusal it can recover from: the valid actions ride on every response.

```json
{ "error": "invalid_transition", "valid_next_actions": ["take_order"] }
```

A smaller example, the same mechanism: an agent ordering coffee, refused when it tries to pay before ordering and recovering from the refusal.

![A coffee-order FSM driven over MCP, with a refusal and recovery](demos/demo.gif)

---

## Primitives at a glance

The vocabulary you meet, in roughly the order you reach for it. Every Theodosia server in `STEP` mode exposes the same four MCP tools regardless of FSM complexity, and a fixed set of `theodosia://` resources for inspection.

- **`mount(application, *, hooks=[...], middleware=[...], upstream=..., personas=...)`**: wraps a Burr `Application` (or factory) as a FastMCP server. Returns the server; call `.run()` to serve, or pass to FastMCP's in-memory `Client` for tests. The optional kwargs forward Burr `LifecycleAdapter` instances, FastMCP `Middleware` instances, upstream MCP clients, and PERSONA.md identity layers without making you reach into the underlying objects.
- **The four-tool surface**: every mounted server exposes `step(action, inputs)`, `reset_session`, `fork_at(sequence_id)`, and `fork_from_past(app_id, sequence_id)`, each carrying FastMCP `ToolAnnotations` (`destructiveHint`, `idempotentHint`, `openWorldHint`) so capable clients can render the right confirmations. The action namespace lives in `step`'s argument schema; FSM complexity changes the schema, not the tool count. FastMCP's `ResourcesAsTools` transform adds two more (`list_resources`, `read_resource`) for clients that don't implement native `resources/read`; the architectural surface is still four.
- **Structured refusals**: `invalid_transition`, `unknown_action`, `validation_failed`, `action_timeout`, `action_error`, plus the fork refusals `cannot_fork_to_refusal`, `unknown_past_run`, and `no_tracker` when the session has no persister or tracker wired. Every refusal carries `valid_next_actions` so the agent self-corrects from the response.
- **`theodosia://` resources**: `graph`, `state`, `next`, `history`, `subruns`, `trace`, `session`. The agent reads state from these instead of guessing.
- **`upstream`**: a Burr action body calling tools on other MCP servers via `call_upstream(server, tool, args)`. The agent never sees those servers; only Theodosia's `step`.
- **`Persona`**: PERSONA.md identity layer mounted as MCP prompts. Same FSM, different actor; same audit trail. Frame-aware placeholders (`{state.x}`, `{action.name}`) interpolate against the live session.
- **`Assembly`**: a frozen bundle of a workflow plus its personas, upstream config, instructions, and metadata. `Assembly(...).serve()` mounts; `from_yaml` loads from disk.
- **`fork_at` / `fork_from_past`**: branch any run at any past sequence id. Replay the prefix, diverge from the chosen point.
- **`theodosia.tracker(project)`**: a Burr `LocalTrackingClient` defaulted to `~/.theodosia` so LLM-driven sessions stay separate from code-driven Burr runs in `~/.burr`. Use it in your builder: `.with_tracker(theodosia.tracker("my-project"))`. Honors `THEODOSIA_HOME` and `build_cli(home=...)`.
- **Hooks**: Burr's lifecycle adapters (`PreRunStepHook`, `PostRunStepHook`, `PreStartStreamHook`, persister hooks, etc.) attach via `mount(..., hooks=[hook1, hook2])` or via `ApplicationBuilder.with_hooks(...)` in your factory. Either path works.
- **Middleware**: FastMCP `Middleware` instances attach via `mount(..., middleware=[mw1, mw2])`. Use for OpenTelemetry spans, rate limiting, structured logging, per-call metrics.
- **`drive_claude(server, anthropic, *, prompt, ...)`**: one-line glue between a mounted server and the Anthropic SDK. Lists the FSM's tools, injects `theodosia://graph` / `state` / `next` into the system prompt, loops turn-by-turn until terminal or `max_turns`. Optional `[claude]` extra.

## What this is not

- Not a workflow engine. Workflows model control flow; Theodosia mounts an existing state machine and gates the agent's access to it.
- Not a tool router. The agent calls `step`; Theodosia decides what's reachable. Routing is a side effect of the graph.
- Not a chat framework. If your problem fits in one conversation, use a chat framework.
- Not an agent harness. The agent and its model live in the client. Theodosia is the server it talks to.
- Not magic. A loose graph produces a loose agent. The rails are only as tight as the FSM you author.

---

## Command line

```bash
theodosia serve module:app                       # mount as MCP server (stdio, default)
theodosia serve module:app --transport http --port 8000   # serve over HTTP instead
theodosia render module:app                      # draw the state machine in the terminal (--mermaid / --dot)
theodosia doctor module:app                      # statically validate the graph; exits nonzero for CI
theodosia sessions show <id>                     # full timeline: per-step state diff + timing; prints Burr UI URL
theodosia sessions show <id> --open              # also open the Burr UI replay in the browser
theodosia sessions diff <a> <b>                  # cross-session: action path divergence + final-state diff
theodosia watch                                  # live-tail a running session
theodosia logs --refusals                        # only the steps that were refused
theodosia status                                 # tracker storage + recent activity snapshot
theodosia verify                                 # check the session's tamper-evident ledger
theodosia primer                                 # 30-second offline tour, no API key needed
theodosia ui                                     # open the Burr UI (auto-bootstraps via uvx, or install `theodosia[ui]`)
```

A downstream package can ship its own command (`my-fsm serve`, `my-fsm doctor`, ...) with `build_cli`.

---

## Observability and replay

A run is not a chat log you have to reconstruct, it is a **replayable artifact**. Every session is recorded through Burr's tracker, so you can replay any finished run step by step, with its state diffs, refusals, and timing:

```bash
theodosia sessions show <session-id>
```
```
 seq  action               state change
  0   start_investigation  incident set, phase=triage, datasources discovered
  1   record_probe         findings=[1], backends=[prometheus]
  2   record_probe         findings=[2], backends=[prometheus, loki]
  3   advance_phase        phase=verify
  4   conclude ✓ (terminal) primary_service=…, root_cause=…
```

Tail a live run (`theodosia watch`), open it in the Burr UI for the transition graph and time-travel, or **fork from any past state** (`fork_at`) to branch the investigation and try a different path. Refusals are recorded too, they appear in the timeline like any other step. A free-ranging agent at the same accuracy hands you a transcript; this hands you the run, replayable and forkable, with proof of which steps were enforced.

![theodosia logs replaying a session timeline, including a refused step](demos/observability.gif)

---

## Documentation

Full docs at **[msradam.github.io/theodosia](https://msradam.github.io/theodosia/)**.

| Section | What it covers |
|---|---|
| [Authoring a graph](https://msradam.github.io/theodosia/authoring/) | Build a Burr Application from scratch and serve it, with the traps newcomers hit |
| [Examples](https://msradam.github.io/theodosia/examples/) | Standalone agents built with Theodosia (Phoebe, triage, deploy-gate, coffee) and the in-repo FSMs. New here? Start with [examples/CURATED.md](examples/CURATED.md). |
| [Architecture](https://msradam.github.io/theodosia/architecture/) | The four-tool surface, structured refusals, how `mount()` drives Burr |
| [What works through mount()](https://msradam.github.io/theodosia/compatibility/) | Typed state, persistence, hooks, parallelism, sub-applications, telemetry |
| [Observability](https://msradam.github.io/theodosia/observability/) | The `theodosia://` resources, the CLI, the Burr UI, OpenTelemetry |
| [Security model](https://msradam.github.io/theodosia/security-model/) | The agent trust boundary: what Theodosia enforces, and what it does not |
| [Case study](https://msradam.github.io/theodosia/case-study/) | Same model, on rails vs free-ranging: where the rails make the agent finish, grader-verified |
| [Research foundation](https://msradam.github.io/theodosia/research-foundation/) | The published evidence behind the design, and what rails do not fix |
| [Driving other MCP servers](https://msradam.github.io/theodosia/upstream/) | `upstream`: a Burr action calling tools on other MCP servers |
| [CLI](https://msradam.github.io/theodosia/cli/) | `serve` / `doctor` / `render` / `sessions` / `watch` / `logs` / `status` / `report` / `primer`, and `build_cli` |
| [Deployment recipes](https://msradam.github.io/theodosia/deployment/) | Ten copy-pasteable configs: Claude Code, Cursor, mcphost, fast-agent, HTTP, SSE, Lambda, Kubernetes, Slack webhook, embedded sub-app |

---

## Compose with Philip

[Philip](https://github.com/msradam/philip) is the sibling library that lifts existing operational artifacts into Burr Applications Theodosia mounts. Philip handles the lift; Theodosia handles the wire. The composition is one line:

```python
import philip, theodosia

# Ansible playbook -> Burr -> MCP server (the canonical path)
theodosia.mount(philip.from_playbook("backup.yml"), name="backup-mcp").run()
```

Philip ships deterministic lifts for Ansible YAML (`from_playbook`), Mermaid stateDiagram-v2 (`from_mermaid`), and Excalidraw sketches (`from_excalidraw().to_burr()`), plus Hamilton-target lifts for Mermaid flowcharts and SQL CTEs.

Ansible's `when:`, `register:`, `notify:`, and handlers preserve into the lifted Burr transitions, so a `step()` from the wrong state refuses with `valid_next_actions` derived from the YAML's real preconditions. Mermaid and Excalidraw lifts work the same way through Theodosia: a branched diagram exposes the branch through `step(action="<state>", inputs={"choice": "<label>"})`, the matching outbound transition becomes the only reachable next action, and the wrong choice refuses the same way the rest of the surface does.

---

## Agents built with Theodosia

Standalone repositories, each a real agent you can clone and run:

| Repo | What it is |
|---|---|
| [Leavitt](https://github.com/msradam/leavitt) | On-call AI diagnostician. Reads metrics, logs, client load, and feature-flag state through MCP; classifies upstream responses (ok / error / malformed) so one bad source cannot poison the diagnosis; degrades or declines under chaos instead of guessing. Read-only by construction. Won the Crusoe track at the DevNetwork AI+ML Hackathon 2026. |
| [Phoebe](https://github.com/msradam/phoebe) | SRE incident-investigation FSM (the hero above). Keeps the full Grafana toolset; the FSM gates the procedure and the audit trail. Ships a Harbor agent for Grafana's o11y-bench. |
| [triage-agent](https://github.com/msradam/triage-agent) | Support triage: investigate before you decide, enforced by the graph. |
| [deploy-gate-agent](https://github.com/msradam/deploy-gate-agent) | A change/deploy gate: ordered gates, a health gate, an audit trail, and a call out to a filesystem MCP server via `upstream`. |
| [coffee-agent](https://github.com/msradam/coffee-agent) | The toy: a coffee-order state machine an LLM drives one enforced step at a time. |

## Examples and tests

[`examples/`](examples/) ships self-contained FSMs (pure-FSM, typed state, hooks, persistence, real shellouts, LLM-in-the-graph, SKILL-to-FSM, upstream, multi-graph), each runnable with `uv run python examples/<file>.py`. Run the suite with `uv run pytest`.

---

## Acknowledgements

Theodosia is glue between two libraries that do the hard parts: [Apache Burr](https://github.com/apache/burr) provides the state-machine `Application`, the transition graph, and the tracking UI; [FastMCP](https://github.com/jlowin/fastmcp) provides the MCP server, the transforms, and the client behind `upstream`. The SKILL demos under `examples/skills/` are reproduced verbatim from Anthropic and Trail of Bits with attribution.

On the name: Theodosia was Aaron Burr's daughter, known for her correspondence with him. The project sits in the same family as Burr and reaches it, which is the role it plays here.

Theodosia is an independent project, not affiliated with or endorsed by the Apache Software Foundation, DAGWorks, the Apache Burr project, or FastMCP.

## License and notice

Apache 2.0. Theodosia is independent open-source work by Adam Munawar Rahman and does not represent the views of IBM Corporation or any other employer. See [NOTICE.md](NOTICE.md).
