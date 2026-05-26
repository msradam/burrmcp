---
title: 'Case study: the rails make the agent finish'
description: 'One incident task, the same model run two ways. Free-ranging it never delivered an answer on any of three runs; on rails the conclude gate forced a correct conclusion. The benchmark grader is the witness.'
---

The same model (Kimi K2.6) was given one incident-investigation task two ways:
free-ranging with the raw Grafana toolset, and driving
[Phoebe](https://github.com/msradam/phoebe), an SRE-investigation state machine
served through Theodosia. Free-ranging, the model investigated and then never
produced an answer, on all three runs. On rails, the `conclude` action is a
gate, so the model had to commit, and it reached the correct root cause.

The quotes below are from [o11y-bench](https://o11ybench.ai/)'s own grader, not
from us.

:::note[Scope]
This is a single illustrative case: one task, and the one failure mode it shows.
It is not a benchmark result. An aggregate comparison across the full
investigation category is pending a clean benchmark run. Rails constrain the
order steps run in; they do not make the model smarter. For where rails help and
where they do not, see the [research foundation](research-foundation.md).
:::

## The task: root-cause a checkout slowdown

**Free-ranging (all three runs scored 0.15).** The model ran many tool calls
across metrics, logs, and traces, then stopped without a conclusion. All three
runs failed the same way. The grader on one run:

> "There is no final response message in the transcript, it ends with tool calls
> and thinking blocks, so there is no characterization of the slowdown presented
> to the user."

Each run passed exactly one of five checks, that it did at least two
observability pulls. It never delivered an answer.

**On rails (scored 1.0).** Driving Phoebe, the model reached the `conclude`
gate and committed:

> "The checkout sluggishness six hours ago was caused by intermittent database
> connection timeouts in order-service ... while payment-service and
> user-service stayed around 0.05 s, confirming the problem is isolated."

It cited p95 latency spiking to 10s, the database-connection-timeout log
pattern, and a representative failing trace ID. All five checks passed.

## Why

Theodosia serves Phoebe's graph so the agent drives it one transition at a time,
and `conclude` is gated: it cannot fire until the verify phase has a confirming
probe. The agent cannot end the session by trailing off. On this task, that
single structural constraint is the difference between many tool calls and no
answer, and a committed, correct root cause, with the same model underneath.

This is the failure the literature names, agents that do not recognize when to
terminate (see the [research foundation](research-foundation.md)), shown on a
real task with the benchmark's own grader as the witness. It is one case; the
aggregate across the category is pending a clean run.

## The receipts

Distilled from the actual recorded run (the tool sequence, the absent or
committed final answer, and the grader's per-check verdicts): see
[`bench/case_studies/evidence.json`](https://github.com/msradam/phoebe/blob/main/bench/case_studies/evidence.json)
in the Phoebe repo. Model: Kimi K2.6 via Together. Harness:
[o11y-bench](https://o11ybench.ai/) investigation category.
