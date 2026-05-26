---
title: 'Benchmark'
description: 'On Grafana o11y-bench, the same model scores higher driving an enforced workflow than running free. Same model, same tasks, same grader.'
---

Theodosia's claim is reliability and accountability, not raw model capability. On
Grafana's [o11y-bench](https://o11ybench.ai/) we can put a number on it: the same
model scores higher driving an enforced workflow than it does running free.

## The result

Kimi K2.6 (open weights), o11y-bench's investigation category (11 multi-step
incident tasks), Pass^3 (consistency across three runs per task), graded by
o11y-bench's own grader:

| Kimi K2.6 with | Investigation Pass^3 |
|---|---|
| Phoebe, an SRE-investigation FSM driven through Theodosia | **0.75** |
| the raw Grafana toolset (o11y-bench's default agent) | 0.68 |

Same model, same tasks, same grader. The only thing that changes is whether the
model drives an enforced workflow or runs free. 33 trials per arm.

This is a preliminary independent run; a leaderboard submission is in progress.

## What this is, and what it is not

- It **is** evidence that an enforced workflow makes a given model more reliable
  on procedural SRE work: the same model gained about seven points of Pass^3 on
  this category by driving the FSM.
- It is **not** a model-versus-model claim. Phoebe is a purpose-built
  investigation agent, so this is not "Kimi beats model X." The honest
  comparison is same-model: Phoebe plus Kimi versus the default agent plus Kimi.
- Phoebe is tuned for SRE investigation and was iterated against this category,
  so read the number as strong on this category, not a universal guarantee. For
  where rails help and where they do not, see the
  [research foundation](research-foundation.md).

## Why it works

Free-ranging, the same model often investigated for dozens of steps and never
produced an answer. On rails, the `conclude` gate made it commit, and the phase
structure made it cover the blast radius first. The
[case study](case-study.md) has three grader-verified pairs and the exact
failure modes, in o11y-bench's own words.

## Method

- Harness: o11y-bench, built on Harbor, against a real Grafana stack with
  synthetic metrics, logs, and traces.
- Metric: Pass^3, the benchmark's headline consistency metric, three runs per task.
- Settings: `timeout_multiplier = 1.0`, no resource overrides, `n_attempts = 3`,
  `n_concurrent = 4`. Concurrency affects only parallelism, not per-trial scoring.
- Agent: [Phoebe](https://github.com/msradam/phoebe) (open source). Model: Kimi
  K2.6 via Together.
- Every step is recorded to a tamper-evident ledger (`theodosia verify`), and the
  full run artifacts are preserved with the result, so the entry is auditable
  end to end.
