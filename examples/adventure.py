"""A tiny text adventure as an FSM, mirroring Burr's
``llm-adventure-game`` example.

Each room is a state. Each direction (or action like ``take_key`` or
``unlock_door``) is a transition gated on the agent being in the
right place and holding the right items. The agent connecting over
MCP can only call moves that are reachable from where it is now,
so a "go_north" call from the wrong room comes back as
``invalid_transition`` with the actually-legal moves listed in the
response.

This is the FSM-as-API pitch in its sharpest form: the agent
navigates a state space the server fully describes. The full topology
is one ``burr://graph`` read away; the agent doesn't have to guess.

Map:

    +------------+        +------------+
    |   foyer    | --N--> |  library   |
    |            |        | (has key)  |
    +-----+------+        +------+-----+
          |                      |
          E                      W
          v                      |
    +------------+        +------+------+
    |   garden   | <--E-- |  hallway    |
    |            |        | (locked     |
    +------------+        |  door east) |
                          +-------------+

Goal: reach the garden via the hallway by unlocking the eastern door
with the key found in the library.

Compare with Burr's
``examples/llm-adventure-game`` for the same pattern driven by an
LLM-internal state machine; here the FSM is the wire-level API.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "adventure-demo"

# ── rooms ───────────────────────────────────────────────────────────


@action(reads=[], writes=["room", "log"])
async def enter_foyer(state: State) -> State:
    """Start in the foyer. Doors lead north (library) and east (garden)."""
    return state.update(room="foyer", log=["You enter the foyer."])


@action(reads=["log"], writes=["room", "log"])
async def go_to_library(state: State) -> State:
    """Walk north into the library. A brass key sits on the table."""
    log = [*state.get("log", []), "You walk north into the library."]
    return state.update(room="library", log=log)


@action(reads=["log"], writes=["room", "log"])
async def go_to_garden_direct(state: State) -> State:
    """Walk east into the garden directly from the foyer."""
    log = [*state.get("log", []), "You walk east into the garden."]
    return state.update(room="garden", log=log)


@action(reads=["log"], writes=["room", "log"])
async def go_to_hallway(state: State) -> State:
    """Walk south from the library into the hallway."""
    log = [*state.get("log", []), "You walk south into the hallway."]
    return state.update(room="hallway", log=log)


@action(reads=["log"], writes=["has_key", "log"])
async def take_key(state: State) -> State:
    """Pick up the brass key in the library."""
    log = [*state.get("log", []), "You pocket the brass key."]
    return state.update(has_key=True, log=log)


@action(reads=["has_key", "log"], writes=["door_unlocked", "log"])
async def unlock_door(state: State) -> State:
    """Unlock the door to the east of the hallway with the brass key."""
    log = [*state.get("log", []), "The brass key fits. The door clicks open."]
    return state.update(door_unlocked=True, log=log)


@action(reads=["log"], writes=["room", "log", "won"])
async def go_to_garden_via_hallway(state: State) -> State:
    """Walk east through the unlocked door into the garden. You win."""
    log = [
        *state.get("log", []),
        "You step through the door and onto the garden's gravel path. Sunlight, finally. You win.",
    ]
    return state.update(room="garden", won=True, log=log)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            enter_foyer=enter_foyer,
            go_to_library=go_to_library,
            go_to_garden_direct=go_to_garden_direct,
            go_to_hallway=go_to_hallway,
            take_key=take_key,
            unlock_door=unlock_door,
            go_to_garden_via_hallway=go_to_garden_via_hallway,
        )
        .with_transitions(
            # From the foyer you can head north (library) or east (garden).
            # The "direct" garden path doesn't lead to the win condition,
            # which the agent has to deduce by reading the map / hints in
            # action docstrings or by trying both.
            ("enter_foyer", "go_to_library", Condition.expr("room == 'foyer'")),
            ("enter_foyer", "go_to_garden_direct", Condition.expr("room == 'foyer'")),
            # In the library: take the key or move on.
            ("go_to_library", "take_key", Condition.expr("room == 'library'")),
            ("go_to_library", "go_to_hallway", Condition.expr("room == 'library'")),
            # After taking the key you can still walk south to the hallway.
            ("take_key", "go_to_hallway", Condition.expr("has_key == True")),
            # In the hallway: unlock the door (needs the key).
            ("go_to_hallway", "unlock_door", Condition.expr("has_key == True")),
            # After unlocking, walk east to the garden.
            (
                "unlock_door",
                "go_to_garden_via_hallway",
                Condition.expr("door_unlocked == True"),
            ),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            room=None,
            has_key=False,
            door_unlocked=False,
            won=False,
            log=[],
        )
        .with_entrypoint("enter_foyer")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="adventure",
        instructions=(
            "A tiny text adventure. Read burr://graph for the map "
            "(actions and their gated transitions). Goal: reach the "
            "garden via the hallway, which requires unlocking the "
            "door, which requires the key, which is in the library. "
            "Each move records to state['log']; read burr://state to "
            "see where you are and what's happened."
        ),
    )


if __name__ == "__main__":
    build_server().run()
