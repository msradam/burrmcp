---
title: Introduction
description: 'Theodosia is a Python adapter that hands your workflow to any AI agent over MCP. The agent can only take steps your workflow allows; every step it takes and every step it tried gets recorded.'
---

Theodosia is a Python adapter library. You write your workflow once as a Burr `Application` (a small Python state machine), and Theodosia serves it over **MCP (Model Context Protocol)** so any AI agent (Claude Code, Cursor, fast-agent, your own loop) can drive it.

The agent can only take steps your workflow allows. The server checks reachability before the action body runs. When the agent tries a step that isn't legal from the current state, it gets a structured refusal naming the legal next moves and a reason. Every step it takes, **and every step it tried but couldn't**, is written to a per-session log you can replay, fork, or chain-verify after the fact.

## What you get

- **A workflow you can version-control.** The Burr `Application` is regular Python: imports, types, tests. Put it in git, write `pytest`, refactor with the IDE.
- **Any MCP client drives it.** No per-client SDK. The same `theodosia serve module:attr` works with Claude Code, Cursor, fast-agent, mcphost, and your own loop.
- **Refused attempts are on the record.** Burr's tracker logs the actions that ran; Theodosia adds a `refusals.jsonl` sidecar, so the trail reflects what the agent *tried*, not its own account.
- **A hash-chained ledger.** Every entry includes the previous hash plus the session's `app_id`, `project`, and `partition_key`. `theodosia verify` recomputes the chain and names the exact line if anything was edited, reordered, or middle-deleted. Set `THEODOSIA_LEDGER_KEY` to switch SHA-256 to HMAC for production.
- **`fork_at` and `fork_from_past`.** Resume or branch from any past step. Useful for retries, what-ifs, and rerunning the same workflow against a different model.

## Try it without an API key

```bash
pip install theodosia
theodosia primer
```

`theodosia primer` walks a bundled coffee-order example in process. No LLM, no network, byte-for-byte the same output every run. It's the first thing to run after install.

## Build your own

[Build your own agent](tutorial.md) takes you through writing the workflow, serving it, and driving it with a real agent. [Authoring a graph](authoring.md) is the reference for the Burr building blocks (`@action`, `Condition`, `with_transitions`). If you already have a Mermaid diagram or an Excalidraw sketch, [Philip](https://github.com/msradam/philip) (a sibling library) lifts those into a Burr `Application` so `theodosia.mount()` can serve them directly.

## Going further

- [Refusals and recovery](refusals.md): the shapes of refusal the agent gets back, and how it self-corrects.
- [Sessions and forking](sessions.md): per-session isolation, `fork_at`, `fork_from_past`, and partition-key binding.
- [Architecture](architecture.md): what `mount()` does and the four MCP tools it registers.
- [Security model](security-model.md): what the ledger does and does not prove.
- [Case study](case-study.md): Kimi K2.6 on Grafana's o11y-bench, on rails vs free-ranging.
