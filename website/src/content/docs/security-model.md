---
title: 'Security model'
description: 'The trust boundary Theodosia enforces, and what it deliberately does not.'
---

Theodosia sits between an AI agent and your workflow. The agent may be wrong,
may hallucinate a tool name, may try to skip a step or finish early. The point
of Theodosia is to make those failures safe and visible rather than silent.

## What is trusted, and what is not

| Component | Trust | Why |
|---|---|---|
| The agent (the model) | **Untrusted** | It is the thing being constrained. Its tool calls are inputs to validate, not instructions to obey. |
| The graph and action bodies | **Trusted** | You wrote them. They run with your process's privileges. |
| Upstream MCP servers (`upstream`) | **Operator-configured** | You choose which servers an action may reach; they are not agent-selected. |
| A `.app` file, if used | **Trusted local** | It execs Python, so it is a local-authoring convenience, not a fetch-and-run format. |

## What Theodosia enforces

- **Reachability.** The agent can only run an action the graph allows from the
  current state. An out-of-order or unknown action comes back as a structured
  [refusal](refusals.md) with the reachable actions; it never executes.
- **State integrity.** State lives on the server, keyed per session. The agent
  cannot assert a state it is not in. "The model can be wrong; it cannot lie
  about state" is a property, not a slogan.
- **Boundary validation.** Inputs are coerced to the declared schema, and
  optional `input_validators` reject bad inputs before the action body runs.
- **An honest record.** Every attempt, including refusals, is recorded to the
  per-session history and Burr's tracker, so the trail reflects what the agent
  tried, not its own account.

## What Theodosia does not do

- It does **not sandbox action bodies.** Your action code runs with full process
  privileges; treat it as you would any code you ship. Theodosia controls *when*
  an action runs, not *what* your code is allowed to do.
- It does **not constrain reasoning inside a valid step.** It removes structural
  failures (skipping, early termination, unverified success); it does not make a
  wrong-but-legal step right.
- It does **not authenticate the agent or the MCP transport.** Run it behind your
  own transport security (stdio in-process, or an authenticated HTTP front).

## Reporting

Report vulnerabilities privately per [SECURITY.md](https://github.com/msradam/theodosia/blob/main/SECURITY.md);
do not open public issues for them.
