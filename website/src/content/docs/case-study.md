---
title: 'Case study: the rails make the agent finish'
description: 'Same model, same incidents, run two ways. Free-ranging it often never delivers an answer; on rails it commits to a conclusion. The benchmark grader is the witness.'
---

The same model (Kimi K2.6) was given the same incident-investigation tasks two
ways: once free-ranging with the raw Grafana toolset, and once driving
[Phoebe](https://github.com/msradam/phoebe), an SRE-investigation state machine
served through Theodosia. One failure mode shows up again and again in the
free-ranging runs: the model investigates, gathers plenty of telemetry, and then
never produces an answer. On rails, the `conclude` action is a gate, so the model
has to commit.

The quotes below are from [o11y-bench](https://o11ybench.ai/)'s own grader, not
from us.

:::note[Scope]
This is a failure-mode case study on selected tasks, not a leaderboard result. It
shows one specific thing the rails fix (an agent that never terminates), with
real recorded runs. For where rails help and where they do not, see the
[research foundation](research-foundation.md). Rails constrain the order steps
run in; they do not make the model smarter.
:::

## The pattern in one line

Free-ranging, the model can stop whenever it likes, and on the hard
investigations it stops *before* it answers. On rails, `conclude` is a required
terminal action gated behind evidence, so trailing off is not an available move.

## Case 1: root-cause analysis

Task: find the root cause of a checkout slowdown.

**Free-ranging (score 0.15).** Forty tool calls across metrics, logs, and traces,
then nothing. The grader:

> "There is no final response message in the transcript, it ends with tool calls
> and thinking blocks, so there is no characterization of the slowdown presented
> to the user."

It passed exactly one of five checks: "did at least two observability pulls." All
three runs of this task failed the same way.

**On rails (score 1.0).** Nineteen tool calls, then the `conclude` gate, then a
committed answer:

> "The checkout slowdown was driven by order-service, where database connection
> timeouts began occurring around six hours ago. This was isolated to
> order-service; payment-service, user-service, and api-gateway showed no
> significant latency elevation."

It cited p95 latency spiking to 10s, the "database connection timeout" log
pattern, and a representative failing trace. Five of five checks passed.

## Case 2: blast radius

Task: state the combined 5xx error share and which service is primary.

**Free-ranging (score 0.0).** Seventeen tool calls, no conclusion. The grader:

> "There is no final response message in the transcript where the assistant
> communicates the combined 5xx share to the user ... the transcript ends during
> the assistant's internal analysis before delivering conclusions to the user."

**On rails (score 1.0).** It concluded with the right number and the right
structure:

> "The combined 5xx share for payment-service and order-service together is 4.75%."

The grader confirmed 4.75% matches the canonical value (0.047535), that
payment-service was correctly named primary (7.17% vs 3.86%), and order-service
correctly called spillover. Four of four checks passed.

## Case 3: triage (the consistency angle)

Task: triage which services are impacted and how.

**Free-ranging.** Across three runs: 0.0, 0.78, 1.0. Two of the three never
finished. The grader on one:

> "The transcript contains no final response to the user; it ends mid-analysis
> with tool results and thinking blocks."

And on another: "the final response text is cut off mid-sentence and never
completes."

**On rails.** Across three runs: 0.78, 0.78, 0.78. Not a perfect score, it missed
the exact 5xx share figure, but it answered every single time: named
payment-service and order-service, separated primary from cascade, cited
timestamps, and gave next steps. The grader passed seven of its eight checks. The
free-ranging agent was a coin flip that often never answered; on rails it finished
on every run.

## Why

Theodosia serves Phoebe's graph so the agent drives it one transition at a time,
and `conclude` is gated: it cannot fire until the verify phase has a confirming
probe. The agent literally cannot end the session by trailing off. That single
structural constraint is the difference between forty tool calls and no answer,
and nineteen tool calls and a correct root cause, with the same model underneath.

This is the failure the literature names (agents that do not recognize when to
terminate; see the [research foundation](research-foundation.md)), shown on real
tasks with the benchmark's own grader as the witness.

## The receipts

These are distilled from the actual recorded runs (tool sequences, final answers
or their absence, and the grader's per-check verdicts): see
[`bench/case_studies/evidence.json`](https://github.com/msradam/phoebe/blob/main/bench/case_studies/evidence.json)
in the Phoebe repo. Model: Kimi K2.6 via Together. Harness:
[o11y-bench](https://o11ybench.ai/) investigation category.
