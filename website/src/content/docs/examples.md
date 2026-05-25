---
title: 'Examples'
description: 'Standalone agents built with Theodosia, plus the in-repo example FSMs.'
---

## Agents built with Theodosia

Each is a standalone repository you can clone and run, a real agent rather than a snippet.

| Repo | What it is |
|---|---|
| [Phoebe](https://github.com/msradam/phoebe) | SRE incident-investigation FSM. The agent keeps the full Grafana toolset; the FSM gates the procedure (triage, diagnose, verify, conclude) and the audit trail, not the tools. Ships a [Harbor](https://harborframework.com/) agent for Grafana's [o11y-bench](https://o11ybench.ai/). |
| [triage-agent](https://github.com/msradam/triage-agent) | Support triage: investigate before you decide, enforced by the state-machine graph. |
| [deploy-gate-agent](https://github.com/msradam/deploy-gate-agent) | A change/deploy gate: ordered gates, a health gate, an audit trail, and a call out to a filesystem MCP server through [`upstream`](upstream.md). |
| [coffee-agent](https://github.com/msradam/coffee-agent) | The toy: a coffee-order state machine an LLM drives one enforced step at a time. The smallest interesting graph. |

## In-repo examples

The [`examples/`](https://github.com/msradam/theodosia/tree/main/examples) directory in the Theodosia repo ships self-contained FSMs covering the surfaces in [What works through mount()](compatibility.md): pure-FSM, typed state, lifecycle hooks, persistence, real shellouts, LLM-in-the-graph, SKILL-to-FSM conversions, `upstream`, and multi-graph. Each runs with `uv run python examples/<file>.py`.

Start from [Authoring a graph](authoring.md) to build your own.
