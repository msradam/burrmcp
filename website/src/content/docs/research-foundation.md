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

MAST (Cemri et al., 2025) is the first taxonomy of why multi-agent
LLM systems fail. It is built from 200 annotated traces across seven
frameworks, with high inter-annotator agreement (Cohen's kappa 0.88).

It names fourteen failure modes in three categories: specification issues,
inter-agent misalignment, and task verification. Four of those modes are
exactly what an enforced state machine removes:

- FM-1.5, unaware of termination conditions
- FM-3.1, premature termination
- FM-3.2, no or incomplete verification
- FM-3.3, incorrect verification

The MAST team's intervention studies point the same way. In the paper,
adding a verification step to a multi-agent coding system (ChatDev, on the
ProgramDev task) yielded a +15.6% improvement in task success, and improving
role specifications added +9.4%. In a follow-up write-up co-published by IBM
and UC Berkeley, a combined architectural intervention (a summarizer agent to
fix context loss plus a stricter state machine to enforce termination) reached
up to a 53% improvement. The pattern is directional: the gains that held came
from architecture (external verification, a termination-enforcing state
machine), not prompt tweaks.

Two cautions on those numbers. They are not observability or IT-Bench results:
the 15.6% is the ChatDev/ProgramDev coding study in the paper, and the 53% is a
combined intervention reported in the MAST team's follow-up blog, not the
peer-reviewed paper and not attributable to the state machine alone. And the
work is UC-Berkeley-led (Cemri et al.), co-published with IBM, not an IBM
Research result.

Microsoft's AIOpsLab found the same shape from the reliability side. The best
agent reached at most about 59% aggregate accuracy (at a 20-step limit), and
for several agents more steps did not help: accuracy plateaued and some
regressed into repetitive error loops, repeating the same failed command many
times. Agents also misread normal system activity as faults, reporting
incidents on no-fault cases.

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
Self-critiquing failed to help reliably and badly underperformed the external
verifier.

DeepMind's "Large Language Models Cannot Self-Correct Reasoning Yet" (Huang
et al., ICLR 2024) shows that intrinsic self-correction, with no external
signal, does not improve accuracy and often degrades it. It draws the line at
external feedback: the gains it could find came from outside the model, and it
attributes prior reported self-correction gains, including Reflexion's, to
oracle labels (telling the model when to stop) rather than to genuine intrinsic
self-correction. The purely introspective variant is the weak case.

Scope matters here. This evidence is strongest where legality is *formally
decidable*, which is exactly the regime of FSM transition legality. The claim
is not "models cannot verify anything." It is "where legality is formally
decidable, an external enforcer beats model self-regulation." That is what the
citations support.

## Reliability is a different axis from capability

The failures Theodosia targets persist at the frontier.

On the tau-bench family, the best model passes about 61% of retail tasks and
about 35% of airline tasks at pass^1, and is inconsistent across repeated runs:
pass^8 falls below 25% on retail. The failures center on policy adherence and
consistency under repetition, alongside reasoning over complex databases and
compound requests, and they have not scaled away.

"Capable but Unreliable: Canonical Path Deviation as a Causal Mechanism of
Agent Failure in Long-Horizon Tasks" (Lee, 2026) measures one mechanism. An
off-canonical tool call at one step raises the probability that the next step
is also off-canonical by 22.7 points, more than doubling the baseline rate: a
self-reinforcing drift away from the task's canonical path. The paper's own
remedy is runtime monitoring and restart, so we cite it for the drift
mechanism, not as an endorsement of this design.

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
- Lee, *Capable but Unreliable: Canonical Path Deviation as a Causal Mechanism of Agent Failure in Long-Horizon Tasks*, arXiv:2602.19008. <https://arxiv.org/abs/2602.19008>
- *Guiding LLMs The Right Way* (constrained decoding), arXiv:2403.06988. <https://arxiv.org/abs/2403.06988>
