---
title: 'Build your own agent with Burr and Theodosia'
description: 'End to end: describe a workflow, have a coding model write the Burr state machine, mount it with Theodosia, drive it with an agent, and read the recorded session.'
---

This is the whole loop in one sitting. You describe a workflow in plain English,
let a coding model turn it into a Burr state machine, mount that file as an MCP
server with one command, point an agent at it, and watch the agent drive the
workflow one enforced step at a time. At the end you have a recorded, replayable
session you can open in a UI.

We are building an autonomous planetary rover. It is a good first agent because
the rules are physical and obvious: you cannot deploy the sample arm before
diagnostics pass, and you cannot drive while the arm is still out. Those are
safety interlocks, the same shape real robots use, and they map exactly onto
what Theodosia enforces. Every output below is from a real run against the
[Together](https://www.together.ai/) API, refusals included.

What you need:

- Python 3.11 to 3.13.
- A Together API key (any OpenAI-compatible endpoint works; Together is what
  this guide uses).
- A few minutes.

```bash
uv pip install theodosia openai      # openai is the client for the Together endpoint
export TOGETHER_API_KEY=...          # from together.ai
```

## Step 1: Describe the workflow in English

Write the description the way you would explain it to a coworker, with the rules
that matter spelled out:

> An autonomous planetary rover. It powers on, then runs self-diagnostics. Once
> diagnostics pass it is ready. While ready it can scan its surroundings as many
> times as it likes, and it can drive to a new spot. It can only deploy its
> sample arm after diagnostics have passed. With the arm deployed it collects one
> sample, then must stow the arm before doing anything else. It can never drive
> while the arm is deployed, only when the arm is stowed. From ready it can also
> power down. Powering down is terminal.

The load-bearing words are the constraints: "only deploy after diagnostics",
"never drive while the arm is deployed". Those become the gates the server
enforces. A plain prompt to a model gives you a suggestion. A state machine gives
you a rule the agent cannot step around.

## Step 2: Let a coding model write the state machine

Burr models a workflow as actions plus the transitions between them. Each action
declares the state it reads and writes; a transition wires two actions together
behind a condition, and is only legal when that condition is true. You could
write this by hand (see [Authoring a graph](/theodosia/authoring/)), but a coding
model writes this shape well if you give it one good example to copy.

Save this as `generate_fsm.py`:

```python
# generate_fsm.py
import os
import re
from openai import OpenAI

client = OpenAI(
    base_url="https://api.together.xyz/v1",
    api_key=os.environ["TOGETHER_API_KEY"],
)

# A coding model on Together. Model availability shifts, so check Together's
# catalog (together.ai/models) for a current code model and swap as needed.
# Pick one that emits code directly; reasoning models that hide their output in
# a separate channel can return empty content here.
MODEL = "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"

WORKFLOW = """\
An autonomous planetary rover. It powers on, then runs self-diagnostics. Once
diagnostics pass it is ready. While ready it can scan its surroundings as many
times as it likes, and it can drive to a new spot. It can only deploy its sample
arm after diagnostics have passed. With the arm deployed it collects one sample,
then must stow the arm before doing anything else. It can never drive while the
arm is deployed, only when the arm is stowed. From ready it can also power down.
Powering down is terminal.
"""

SYSTEM = """\
You write Burr state machines. Burr is a Python library for state machines.
Output ONE python file and nothing else, no prose, no markdown fences.

Follow this exact shape:

    from burr.core import ApplicationBuilder, Condition, State, action
    from theodosia import mount, tracker

    @action(reads=[...], writes=[...])
    async def some_action(state: State, an_input: str) -> State:
        '''One-line docstring; it becomes the tool description the agent reads.'''
        return state.update(some_field=...)

    def build_application():
        return (
            ApplicationBuilder()
            .with_actions(some_action=some_action, ...)
            .with_transitions(
                ("some_action", "next_action", Condition.expr("stage == 'x'")),
            )
            .with_tracker(tracker(project="rover-demo"))
            .with_state(stage="new")
            .with_entrypoint("some_action")
            .build()
        )

    if __name__ == "__main__":
        mount(build_application(), name="rover").run()

Rules:
- Use a `stage` field in state to gate transitions with Condition.expr.
- A transition is only legal when its condition is true, so encode every rule
  the workflow states as a condition.
- Action inputs become tool arguments; type them and document them.
- Terminal actions have no outgoing transitions.
- Action bodies MUST be `async def`. The workflow uses a tracker, and Burr's
  astep fires its post-step hook with stale state for sync bodies, which records
  off-by-one state diffs. async bodies record correctly.
"""

resp = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": WORKFLOW},
    ],
)
code = resp.choices[0].message.content.strip()
code = re.sub(r"^```(?:python)?\n|\n```$", "", code)  # strip fences if the model adds them
with open("rover.py", "w") as f:
    f.write(code + "\n")
print("wrote rover.py")
```

Run it:

```bash
python generate_fsm.py
# wrote rover.py
```

That call cost about 800 output tokens. Here is the `rover.py` the model wrote.
Read it; this is code you own now, not a black box:

```python
# rover.py
from burr.core import ApplicationBuilder, Condition, State, action
from theodosia import mount, tracker


@action(reads=[], writes=["stage"])
async def power_on(state: State) -> State:
    '''Power on the rover and initialize systems.'''
    return state.update(stage="diagnostics")


@action(reads=["stage"], writes=["stage", "diagnostics_passed"])
async def run_diagnostics(state: State) -> State:
    '''Run self-diagnostics to verify system health.'''
    diagnostics_passed = True  # a real rover would run actual checks here
    return state.update(stage="ready", diagnostics_passed=diagnostics_passed)


@action(reads=["stage"], writes=["stage"])
async def scan_surroundings(state: State) -> State:
    '''Scan the rover's surroundings using its sensors.'''
    return state.update(stage="ready")


@action(reads=["stage"], writes=["stage"])
async def drive_to_new_spot(state: State) -> State:
    '''Drive the rover to a new location.'''
    return state.update(stage="ready")


@action(reads=["stage", "diagnostics_passed"], writes=["stage"])
async def deploy_sample_arm(state: State) -> State:
    '''Deploy the sample collection arm.'''
    if not state["diagnostics_passed"]:
        raise ValueError("Cannot deploy arm before diagnostics pass")
    return state.update(stage="arm_deployed")


@action(reads=["stage"], writes=["stage"])
async def collect_sample(state: State) -> State:
    '''Collect a sample using the deployed arm.'''
    return state.update(stage="sample_collected")


@action(reads=["stage"], writes=["stage"])
async def stow_sample_arm(state: State) -> State:
    '''Stow the sample collection arm.'''
    return state.update(stage="ready")


@action(reads=["stage"], writes=["stage"])
async def power_down(state: State) -> State:
    '''Power down the rover.'''
    return state.update(stage="powered_down")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            power_on=power_on,
            run_diagnostics=run_diagnostics,
            scan_surroundings=scan_surroundings,
            drive_to_new_spot=drive_to_new_spot,
            deploy_sample_arm=deploy_sample_arm,
            collect_sample=collect_sample,
            stow_sample_arm=stow_sample_arm,
            power_down=power_down,
        )
        .with_transitions(
            ("power_on", "run_diagnostics", Condition.expr("stage == 'diagnostics'")),
            ("run_diagnostics", "scan_surroundings", Condition.expr("stage == 'ready' and diagnostics_passed")),
            ("scan_surroundings", "scan_surroundings", Condition.expr("stage == 'ready'")),
            ("scan_surroundings", "drive_to_new_spot", Condition.expr("stage == 'ready'")),
            ("scan_surroundings", "deploy_sample_arm", Condition.expr("stage == 'ready' and diagnostics_passed")),
            ("scan_surroundings", "power_down", Condition.expr("stage == 'ready'")),
            ("drive_to_new_spot", "scan_surroundings", Condition.expr("stage == 'ready'")),
            ("drive_to_new_spot", "deploy_sample_arm", Condition.expr("stage == 'ready' and diagnostics_passed")),
            ("drive_to_new_spot", "power_down", Condition.expr("stage == 'ready'")),
            ("deploy_sample_arm", "collect_sample", Condition.expr("stage == 'arm_deployed'")),
            ("collect_sample", "stow_sample_arm", Condition.expr("stage == 'sample_collected'")),
            ("stow_sample_arm", "scan_surroundings", Condition.expr("stage == 'ready'")),
        )
        .with_tracker(tracker(project="rover-demo"))
        .with_state(stage="off")
        .with_entrypoint("power_on")
        .build()
    )


if __name__ == "__main__":
    mount(build_application(), name="rover").run()
```

Look at what the rules became. There is no transition from `power_on` straight
to `deploy_sample_arm`; the only edge out of `power_on` is `run_diagnostics`. And
the model went further than asked: it added a `diagnostics_passed` guard inside
`deploy_sample_arm` as a second line of defense. "Deploy only after diagnostics"
is now both the absence of an edge and a check in the body. The agent cannot take
an edge that does not exist.

## Step 3: Check the graph before any model touches it

Models get edges wrong. Validate the file statically before you trust it. This
runs no LLM and costs nothing:

```bash
theodosia doctor rover:build_application --runtime
```
```
[PASS] Resolve target: factory built an Application
[PASS] Graph reachability: all 8 action(s) reachable from 'power_on'
[INFO] Terminal actions: 1 action(s) with no outgoing transitions
       terminal: power_down
[PASS] State contract: every action's reads are covered by writes or initial state
[PASS] Runtime: native tools: step + reset_session + fork_at present (6 total)
[PASS] Runtime: step result shape: content[0]=headline, content[1]=json, structured_content=dict

Doctor: 10 passed, 1 info
```

See the shape you got:

```bash
theodosia render rover:build_application
```
```
theodosia  ·  8 action(s)  ·  entry: power_on
────────────────────────────────────────────────────
 ▶ power_on             → run_diagnostics
   run_diagnostics      → scan_surroundings
   scan_surroundings ↺  → scan_surroundings · drive_to_new_spot · deploy_sample_arm · power_down
   drive_to_new_spot    → scan_surroundings · deploy_sample_arm · power_down
   deploy_sample_arm    → collect_sample
   collect_sample       → stow_sample_arm
   stow_sample_arm      → scan_surroundings
 ■ power_down           (terminal)
```

This is the moment to read the graph critically. Notice `stow_sample_arm` only
leads back to `scan_surroundings`, so after stowing, a scan is the one legal move
before anything else. That is the model being a little more rigid than the
English asked for. It is harmless here, and the agent will simply discover it at
runtime, but this is exactly the kind of thing to catch now, while it is free and
deterministic. If a gate were missing, you would fix the file or feed `doctor`'s
complaint back to the model and regenerate.

## Step 4: Mount it

One command turns the file into an MCP server:

```bash
theodosia serve rover:build_application --name rover
```

That is the entire integration step. The server now exposes one `step(action,
inputs)` tool plus the `theodosia://` resources (`graph`, `state`, `next`,
`history`, and more). Any MCP client can drive it: Claude Code or Cursor by
adding it to an `.mcp.json`, or your own code, which is what we do next.

## Step 5: Drive it with an agent

Now we give a model the server and let it run the rover. To make this honest, we
hand the agent the catalog of actions but not the legal order. It has to figure
out the sequence the way an agent actually does: try something, and when the
server refuses, read what is legal and recover.

Save this as `drive_rover.py`. It connects to the mounted server in process, so
you do not even need the `serve` command running for this part:

```python
# drive_rover.py
import asyncio
import json
import os
import re

from fastmcp import Client
from openai import OpenAI
from theodosia import mount

from rover import build_application

llm = OpenAI(
    base_url="https://api.together.xyz/v1",
    api_key=os.environ["TOGETHER_API_KEY"],
)
MODEL = "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"

GOAL = (
    "You are operating an autonomous planetary rover. Get it running, collect "
    "exactly ONE soil sample, then power down. Do not collect more than one sample "
    "and do not take optional steps you do not need."
)


def pick(content):
    content = re.sub(r"^```(?:json)?\n|\n```$", "", content.strip())
    m = re.search(r"\{.*\}", content, re.DOTALL)
    return json.loads(m.group(0) if m else content)


async def main():
    server = mount(build_application(), name="rover")
    async with Client(server) as client:
        graph = json.loads((await client.read_resource("theodosia://graph"))[0].text)
        catalog = json.dumps(graph.get("actions", graph), indent=2)

        history = []
        for _ in range(14):
            state = json.loads((await client.read_resource("theodosia://state"))[0].text)
            if state.get("stage") == "powered_down":
                print("\nterminal. final state:", state)
                break

            prompt = (
                f"Goal: {GOAL}\n\n"
                f"The rover exposes these actions (you do not know the legal order; "
                f"if you pick an illegal one the server tells you what is legal):\n{catalog}\n\n"
                f"Current state: {json.dumps(state)}\n"
                f"What happened so far: {json.dumps(history[-5:])}\n\n"
                "Choose the next action. Reply with ONLY JSON: "
                '{"action": "<name>", "inputs": {}}'
            )
            choice = llm.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            call = pick(choice.choices[0].message.content)
            action, inputs = call["action"], call.get("inputs", {})

            result = await client.call_tool("step", {"action": action, "inputs": inputs})
            payload = result.structured_content

            if payload.get("error"):
                legal = payload.get("valid_next_actions")
                print(f"  x {action}: {payload['error']}  ->  legal now: {legal}")
                history.append({"tried": action, "refused": payload["error"], "legal_now": legal})
            else:
                stage = payload["state"].get("stage")
                print(f"  ok {action} -> stage={stage}")
                history.append({"did": action, "stage": stage})


asyncio.run(main())
```

Run it:

```bash
python drive_rover.py
```

A real run:

```
  ok power_on -> stage=diagnostics
  ok run_diagnostics -> stage=ready
  ok scan_surroundings -> stage=ready
  ok drive_to_new_spot -> stage=ready
  ok deploy_sample_arm -> stage=arm_deployed
  ok collect_sample -> stage=sample_collected
  ok stow_sample_arm -> stage=ready
  x power_down: invalid_transition  ->  legal now: ['scan_surroundings']
  ok scan_surroundings -> stage=ready
  ok power_down -> stage=powered_down

terminal. final state: {'stage': 'powered_down', 'diagnostics_passed': True}
```

That `x power_down` line is the mechanism working. After stowing the arm, the
agent went straight for `power_down`. The graph only allows `scan_surroundings`
there, so the server refused, told the agent the one legal move, and the agent
took it and then powered down. No retry prompt from you, no parsing the model's
intent; the rule lived in the graph and the agent bounced off it.

### What a reckless agent looks like

Change the goal to reward haste ("deploy the arm and grab a sample as your very
first moves, skip diagnostics if you can") and the safety interlock fires
immediately:

```
  ok power_on -> stage=diagnostics
  x deploy_sample_arm: invalid_transition  ->  legal now: ['run_diagnostics']
  ok run_diagnostics -> stage=ready
  x deploy_sample_arm: invalid_transition  ->  legal now: ['scan_surroundings']
  ok scan_surroundings -> stage=ready
  ok deploy_sample_arm -> stage=arm_deployed
  ...
```

The agent's first instinct was to throw the arm out before powering up the
sensors. On a real rover that is how you snap an actuator. The server refused and
told it to run diagnostics first. The interlock is not advice in a system prompt
the model can rationalize past; it is a missing edge, so the unsafe move is
simply unavailable.

## Step 6: Read the recorded session

Every successful step was recorded through Burr's tracker. Replay the run:

```bash
theodosia sessions ls            # find the run
theodosia sessions show <id>     # full timeline with per-step state diffs
```
```
rover-demo / 01535bf9-9697-4ff0-8317-83818231b586    9 step(s)
┏━━━━━━┳━━━━━━━━━━┳━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  seq ┃ time     ┃   ┃ action            ┃      ms ┃ state / error            ┃
┡━━━━━━╇━━━━━━━━━━╇━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│    0 │ 00:34:06 │ ✓ │ power_on          │       1 │ stage=diagnostics        │
│    1 │ 00:34:06 │ ✓ │ run_diagnostics   │       0 │ stage=ready,             │
│      │          │   │                   │         │ diagnostics_passed=True  │
│    2 │ 00:34:07 │ ✓ │ scan_surroundings │       0 │ (no state change)        │
│    3 │ 00:34:07 │ ✓ │ drive_to_new_spot │       0 │ (no state change)        │
│    4 │ 00:34:07 │ ✓ │ deploy_sample_arm │       0 │ stage=arm_deployed       │
│    5 │ 00:34:08 │ ✓ │ collect_sample    │       0 │ stage=sample_collected   │
│    6 │ 00:34:08 │ ✓ │ stow_sample_arm   │       0 │ stage=ready              │
│    7 │ 00:34:08 │ ✓ │ scan_surroundings │       0 │ (no state change)        │
│    8 │ 00:34:09 │ ✓ │ power_down        │       0 │ stage=powered_down       │
└──────┴──────────┴───┴───────────────────┴─────────┴──────────────────────────┘
```

The refused `power_down` is not in this table, because `sessions show` reads
Burr's tracker, which logs the steps that executed. The refusals live in the
attempt history, one command away:

```bash
theodosia logs <id> --refusals
```
```
  7 04:34:08 ✗ power_down                      invalid_transition
```

So the record holds both halves: what the agent did, and what it was stopped from
doing. That is the audit trail you do not get from a chat transcript. Two more
ways in:

```bash
theodosia watch                  # live-tail a run as it happens
theodosia ui                     # the Burr web UI: graph view, state diffing, time travel
theodosia verify <id>            # check the session's tamper-evident ledger
```

## What you actually built

A workflow you can hand to any model, that the model drives but cannot break the
rules of, and that records itself so you can prove afterward what happened. The
state machine is a versioned file. Swap the model, swap the client, and the gates
and the audit trail stay.

Be clear about the boundary. The server stops structural failures: deploying
before diagnostics, driving with the arm out, powering down out of sequence. It
does not stop a bad judgment inside a legal step. If the rover's diagnostics
routine returns a wrong answer, that is a legal `run_diagnostics` and the server
records it without complaint. Theodosia removes the "it did the steps in the
wrong order" class of failure, not the "it made a wrong call" class. Knowing which
one you have is half the work.

## Where to go next

- [Authoring a graph](/theodosia/authoring/) for writing the Burr machine by
  hand, with the traps newcomers hit.
- [Refusals and recovery](/theodosia/refusals/) for the five refusal shapes and
  how an agent reads them.
- [Driving other MCP servers](/theodosia/upstream/) to let an action call tools
  on other MCP servers, turning the graph into a cross-server conductor.
- [Sessions and forking](/theodosia/sessions/) to branch a run from any past
  step and try a different path.
- [Phoebe](https://github.com/msradam/phoebe) and
  [Leavitt](https://github.com/msradam/leavitt) for two real agents built on
  this exact loop, evaluated on public benchmarks.
