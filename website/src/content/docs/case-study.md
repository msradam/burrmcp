---
title: 'Case study: Kimi K2.6 on o11y-bench, free-ranging vs gated'
description: 'Two grader-verified incident tasks: Kimi K2.6 run free-ranging with the raw Grafana toolset versus gated through a Burr FSM mounted with Theodosia.'
---

The same model (Kimi K2.6) was given real incident-investigation tasks two
ways: free-ranging with the raw Grafana toolset, and driving
[Phoebe](https://github.com/msradam/phoebe), an SRE-investigation state machine
served through Theodosia. One failure mode recurs in the free-ranging runs: the
model investigates, gathers telemetry, and then **trails off without delivering
an answer**. On rails, the `conclude` action is a gate, so the model has to
commit. Two tasks below show this, one where free-ranging fails every time, one
where it fails intermittently.

The quotes are from [o11y-bench](https://o11ybench.ai/)'s own grader, not from us.

:::note[Scope]
These are two selected tasks where the failure the rails fix (an agent that
never terminates) dominates. They are not an aggregate result across the
category; a full benchmark comparison is pending a clean run. Rails constrain
the order steps run in and force termination; they do not make the model
smarter. For where rails help and where they do not, see the
[research foundation](research-foundation.md).
:::

## Case 1: free-ranging never finishes (chronic)

Task: root-cause a checkout slowdown.

**Free-ranging, all three runs scored 0.15.** The model ran dozens of tool calls
across metrics, logs, and traces, then stopped without a conclusion. All three
runs failed the same way. The grader on one:

> "There is no final response message in the transcript, it ends with tool calls
> and thinking blocks, so there is no characterization of the slowdown presented
> to the user."

**On rails, scored 1.0.** The `conclude` gate forced a committed answer:

> "The checkout sluggishness six hours ago was caused by intermittent database
> connection timeouts in order-service ... while payment-service and
> user-service stayed around 0.05 s, confirming the problem is isolated."

It cited p95 latency spiking to 10s, the database-connection-timeout log
pattern, and a representative trace ID. All five checks passed.

## Case 2: free-ranging finishes inconsistently (intermittent)

Task: identify the owner and trigger of a cache-refresh lag incident.

This is the consistency angle. Free-ranging scored **1.0, 0.0, 1.0** across three
runs: it solved it twice, but on the third run it trailed off mid-investigation
and delivered nothing, so it fails Pass^3 (which requires all three runs to
pass). The grader on the failed run:

> "the final response was never delivered; the transcript ends mid-investigation
> with tool calls."

**On rails, it scored 1.0 on all three runs.** The gate makes the agent commit
every time:

> "The earlier cache-refresh problem was owned by user-service, specifically its
> auth-cache-refresh background job. Cache refresh lag reached approximately 530
> seconds at peak, with up to 862 stale keys logged by user-service ..."

Same model, same task. The free-ranging agent is capable but unreliable, a coin
that lands wrong often enough to fail; the gated agent finishes on every run.

## Why

Theodosia serves Phoebe's graph so the agent drives it one transition at a time,
and `conclude` is gated: it cannot fire until the verify phase has a confirming
probe. The agent cannot end the session by trailing off. That single structural
constraint is the difference between dozens of tool calls and no answer, and a
committed, correct conclusion, with the same model underneath.

This is the failure the literature names, agents that do not recognize when to
terminate (see the [research foundation](research-foundation.md)), shown on real
tasks with the benchmark's own grader as the witness. These are two illustrative
cases; the aggregate across the category is pending a clean run.

## The receipts

Distilled from the actual recorded runs (rewards, the absent or committed final
answer, and the grader's per-check verdicts) for both tasks: see
[`bench/case_studies/evidence.json`](https://github.com/msradam/phoebe/blob/main/bench/case_studies/evidence.json)
in the Phoebe repo. Model: Kimi K2.6 via Together. Harness:
[o11y-bench](https://o11ybench.ai/) investigation category.
