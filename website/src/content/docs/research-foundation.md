---
title: 'Research foundation'
description: 'The published evidence behind driving a state machine from outside the model: why structure, external verification, and audit beat prompt-level fixes.'
---

Theodosia makes one design choice: the model drives a finite state machine
from outside, and a server enforces which transitions are legal. This page
collects the published evidence behind that choice.

The short version, in three claims:

1. The failures that sink agents on procedural work are structural, not gaps
   in intelligence.
2. The verifier has to sit outside the thing it verifies.
3. Reliability, enforceability, and auditability are separate axes from raw
   capability. They do not improve just because the model does.

## The failures are structural

MAST (Cemri et al., 2025) is the first empirical taxonomy of why multi-agent
LLM systems fail. It is built from over 200 annotated traces across seven
frameworks, with high inter-annotator agreement (Cohen's kappa 0.88).

It names fourteen failure modes in three categories: specification issues,
inter-agent misalignment, and task verification. Four of those modes are
exactly what an enforced state machine removes:

- FM-1.5, unaware of termination conditions
- FM-3.1, premature termination
- FM-3.2, no or incomplete verification
- FM-3.3, incorrect verification

IBM Research and UC Berkeley applied MAST to enterprise IT tasks in IT-Bench.
They report the contrast that motivates this design. Prompt-level
interventions capped at roughly 15.6% improvement. Architectural
interventions, including enforcing stricter state machines for termination,
reached roughly 53%, about 3.4 times more effective. Their recommendations
match the Theodosia thesis: never let the model grade its own homework,
require hard evidence before exit, use deterministic state machines for
termination.

One attribution note. The 15.6% vs 53% framing is from the IBM and UC
Berkeley write-up, not the MAST paper itself. The MAST paper's own
intervention case studies report smaller tactical gains and conclude that
more substantial, structural improvements are needed.

Microsoft's AIOpsLab found the same shape from the reliability side. The
best agent cleared about 59% of operations tasks, and more steps did not
help. Agents plateaued or regressed into repetitive error loops, and misread
normal system activity as faults. More autonomy made the problem worse.

## A verifier has to sit outside the thing it verifies

This is the architectural core, and the best-supported claim in the
literature.

Kambhampati's LLM-Modulo framework argues that LLMs are strong *generators*
and unreliable *verifiers*. Soundness has to come from an external checker in
a generate-test loop, never from the model judging itself.

Valmeekam et al. measured it. GPT-4 used as a plan verifier scored 61%
accuracy with an 84% false-positive rate: it waved through 38 of 45 invalid
plans. A GPT-4 generate-then-self-critique loop reached 55%. The same
generator paired with an external sound verifier (VAL) reached 88%.
Self-critiquing did not help. It hurt.

DeepMind's "Large Language Models Cannot Self-Correct Reasoning Yet" (Huang
et al., ICLR 2024) shows that intrinsic self-correction, with no external
signal, does not improve accuracy and often degrades it. Gains reported
elsewhere leaked from oracle labels telling the model when to stop. The
methods that do reliably help (Reflexion, ReAct) all lean on an *external*
signal: test results, tool output, environment state. The purely
introspective variant is the weak case.

Scope matters here. This evidence is strongest where legality is *formally
decidable*, which is exactly the regime of FSM transition legality. The claim
is not "models cannot verify anything." It is "where legality is formally
decidable, an external enforcer beats model self-regulation." That is what the
citations support.

## Reliability is a different axis from capability

The failures Theodosia targets persist at the frontier.

On the tau-bench family, even the strongest models pass under half the tasks
and are inconsistent across repeated runs (pass^k drops steeply). The
failures are policy adherence and consistency under repetition, not missing
intelligence, and they have not scaled away.

"Capable but Unreliable" (Feb 2026) measures the mechanism. Each off-policy
tool call raises the probability that the next one is off-policy by 22.7
points: a self-reinforcing drift. The paper argues for external,
state-machine-like path constraints. That is the structural case for an
enforced graph, stated independently of any one benchmark.

## Theodosia is constrained decoding, lifted to the action level

There is a clean precedent for the mechanism.

Token-level constrained decoding (grammar, regex, or context-free-grammar
constrained generation) guarantees the model can only emit legal strings. An
out-of-band automaton admits or rejects each token.

Theodosia performs the same move one layer up. The model proposes a
transition, and an out-of-band state machine admits or refuses it. The model
proposes; the controller disposes. The technique is well understood at the
token level. Theodosia applies it to actions in a workflow.

## What this design does not claim

Stating the boundary makes the rest credible.

The research is clear that scaffolding's *raw accuracy* benefit shrinks as
models improve. Structured prompting gains average a few points and taper
with scale, and MAST's own structural redesigns yielded limited gains with
the same underlying model. There is also a "verifier tax": aggressive
external gating can cost task success at long horizons.

So Theodosia does not claim to make a model smarter. A wrong-but-legal step
is still wrong. The graph constrains the order steps run in, not the
reasoning inside a step.

Its value is on the axes that do not erode with capability:

- **Enforcement is capability-orthogonal.** This is capability control in
  Bostrom's sense: restricting what a system can do regardless of how capable
  it is. A more capable model that a prompt can steer is also more capable of
  being steered off-policy. A server that refuses an illegal transition is
  invariant to model capability.
- **Audit and override are mandates, not preferences.** The EU AI Act
  requires high-risk systems to be designed for human oversight (Article 14)
  and to keep automatic event logs (Article 12). The NIST AI Risk Management
  Framework embeds oversight across its functions. A more capable model does
  not discharge a logging or override obligation. An external,
  transition-enforcing, logging server is the compliance artifact.

This is why the design ages with model capability rather than against it. As
agents become more autonomous, "the model is the agent, I need to govern it"
becomes the more relevant problem, not less. Theodosia is a position on the
autonomy slider chosen for domains where silent failure is unacceptable, not
a bet that models stay weak.

## Where this sits in the landscape

Agent frameworks overwhelmingly run the loop themselves and invoke the model
as a subroutine inside a node. LangGraph, Burr, Temporal, AWS Step Functions,
Semantic Kernel, AutoGen, CrewAI, the OpenAI Agents SDK and Assistants API,
DSPy, and Pydantic-AI all work this way.

Exposing a graph's individual transitions as an external surface that an LLM
client drives, with the server refusing out-of-order moves, appears only in a
small cluster of MCP-native projects. Theodosia's specific combination is its
distinguishing contribution: lifting an existing framework's state-machine
`Application` (Burr) into MCP so each action becomes a named, condition-gated
transition, with structured refusals that carry the reachable actions for
self-correction.

## References

- Cemri et al., *Why Do Multi-Agent LLM Systems Fail?* (MAST), arXiv:2503.13657. <https://arxiv.org/abs/2503.13657>
- IBM Research and UC Berkeley, *Diagnosing Why Enterprise Agents Fail Using IT-Bench and MAST*. <https://huggingface.co/blog/ibm-research/itbenchandmast>
- Chen et al., *AIOpsLab*, arXiv:2501.06706. <https://arxiv.org/abs/2501.06706>
- Kambhampati et al., *LLMs Can't Plan, But Can Help Planning in LLM-Modulo Frameworks*, arXiv:2402.01817. <https://arxiv.org/abs/2402.01817>
- Valmeekam et al., *Can LLMs Really Improve by Self-critiquing Their Own Plans?*, arXiv:2310.08118. <https://arxiv.org/abs/2310.08118>
- Huang et al., *Large Language Models Cannot Self-Correct Reasoning Yet*, ICLR 2024, arXiv:2310.01798. <https://arxiv.org/abs/2310.01798>
- Yao et al., *ReAct*, arXiv:2210.03629. <https://arxiv.org/abs/2210.03629>
- Shinn et al., *Reflexion*, arXiv:2303.11366. <https://arxiv.org/abs/2303.11366>
- *τ-bench*, arXiv:2406.12045. <https://arxiv.org/abs/2406.12045>
- *Capable but Unreliable*, arXiv:2602.19008. <https://arxiv.org/abs/2602.19008>
- *Guiding LLMs The Right Way* (constrained decoding), arXiv:2403.06988. <https://arxiv.org/abs/2403.06988>
