"""The Proposal: Chekhov's one-act, restaged.

Public-domain source: Anton Chekhov, *Предложение* (1889). Lomov has
come to ask for Natalya's hand. To reach an acceptance he must greet
her father, broach the subject, survive an argument about the Oxen
Meadows, propose, and then survive a second argument about hunting
dogs. Press the same dispute three times and his nerves give out.

Two terminals: ``accept`` and ``collapse``. No LLM, no network ---
Chekhov's structure is the gating.

Run as a server:

    python examples/the_proposal.py

Or:

    theodosia serve the_proposal:build_application --app-dir examples
"""

from __future__ import annotations

from typing import Literal

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ServingMode, mount, tracker

_TRACKER_PROJECT = "the-proposal-demo"
_NERVES = 3  # Press the same argument this many times and Lomov collapses.


@action(reads=[], writes=["scene", "temper"])
def arrive(state: State) -> State:
    """Lomov enters the Chubukov drawing room, holding his hat."""
    return state.update(scene="drawing_room", temper=0)


@action(reads=["scene"], writes=["greeted"])
def greet_chubukov(state: State) -> State:
    """Polite small talk. Chubukov assumes Lomov has come to borrow money."""
    return state.update(greeted=True)


@action(reads=["greeted"], writes=["topic"])
def attempt_proposal(state: State) -> State:
    """Lomov broaches the question. He gets as far as the family land."""
    return state.update(topic="oxen_meadows")


@action(reads=["temper"], writes=["temper", "oxen_meadows_settled"])
def dispute_oxen_meadows(
    state: State,
    position: Literal["yield", "press"] = "press",
) -> State:
    """Whose family the Oxen Meadows belong to.

    Args:
        position: ``"yield"`` resolves the dispute; ``"press"`` escalates.
            Three presses and only ``collapse`` is reachable next.
    """
    if position == "yield":
        return state.update(temper=0, oxen_meadows_settled=True)
    return state.update(temper=state["temper"] + 1)


@action(reads=["oxen_meadows_settled"], writes=["proposal_offered", "temper"])
def propose(state: State) -> State:
    """Lomov manages, at last, to ask. Natalya is on the edge of yes."""
    return state.update(proposal_offered=True, temper=0)


@action(reads=["temper"], writes=["temper", "dogs_settled"])
def dispute_dogs(
    state: State,
    position: Literal["yield", "press"] = "press",
) -> State:
    """Whose hunting dog is the better animal.

    Args:
        position: ``"yield"`` resolves the dispute; ``"press"`` escalates.
            Three presses and only ``collapse`` is reachable next.
    """
    if position == "yield":
        return state.update(dogs_settled=True)
    return state.update(temper=state["temper"] + 1)


@action(reads=["proposal_offered"], writes=["accepted"])
def accept(state: State) -> State:
    """Natalya accepts the proposal she had nearly forgotten was on the table. Terminal."""
    return state.update(accepted=True)


@action(reads=["temper"], writes=["collapsed"])
def collapse(state: State) -> State:
    """Lomov's nerves give out. The doctor is sent for. Terminal."""
    return state.update(collapsed=True)


def build_application():
    """Build the Burr Application for the one-act."""
    greeted = Condition.expr("greeted == True")
    oxen_settled = Condition.expr("oxen_meadows_settled == True")
    dogs_settled = Condition.expr("dogs_settled == True")
    nerves_broken = Condition.expr(f"temper >= {_NERVES}")
    oxen_unresolved = Condition.expr(f"temper < {_NERVES} and oxen_meadows_settled == False")
    dogs_unresolved = Condition.expr(f"temper < {_NERVES} and dogs_settled == False")

    return (
        ApplicationBuilder()
        .with_actions(
            arrive=arrive,
            greet_chubukov=greet_chubukov,
            attempt_proposal=attempt_proposal,
            dispute_oxen_meadows=dispute_oxen_meadows,
            propose=propose,
            dispute_dogs=dispute_dogs,
            accept=accept,
            collapse=collapse,
        )
        .with_transitions(
            ("arrive", "greet_chubukov"),
            ("greet_chubukov", "attempt_proposal", greeted),
            ("attempt_proposal", "dispute_oxen_meadows"),
            # Oxen Meadows: yield resolves it, press escalates, and once
            # nerves are broken there is no resolving anything else.
            ("dispute_oxen_meadows", "collapse", nerves_broken),
            ("dispute_oxen_meadows", "propose", oxen_settled),
            ("dispute_oxen_meadows", "dispute_oxen_meadows", oxen_unresolved),
            # Once proposed, the second argument starts.
            ("propose", "dispute_dogs"),
            ("dispute_dogs", "collapse", nerves_broken),
            ("dispute_dogs", "accept", dogs_settled),
            ("dispute_dogs", "dispute_dogs", dogs_unresolved),
        )
        .with_tracker(tracker(project=_TRACKER_PROJECT))
        .with_state(
            scene="outside",
            greeted=False,
            topic=None,
            temper=0,
            oxen_meadows_settled=False,
            proposal_offered=False,
            dogs_settled=False,
            accepted=False,
            collapsed=False,
        )
        .with_entrypoint("arrive")
        .build()
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the one-act as an MCP server."""
    return mount(
        build_application,
        mode=mode,
        name="the-proposal",
        instructions=(
            "Chekhov's one-act, gated. Walk: arrive -> greet_chubukov -> "
            "attempt_proposal -> dispute_oxen_meadows(position) -> propose "
            "-> dispute_dogs(position) -> accept. position='yield' resolves "
            "the current dispute; position='press' escalates. Three "
            "escalations in either dispute and only collapse is reachable. "
            "Read theodosia://state for what is happening; theodosia://next "
            "for what can happen next."
        ),
    )


if __name__ == "__main__":
    build_server().run()
