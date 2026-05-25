"""D&D-flavoured character creation as an FSM.

A six-stage guided wizard. Each stage writes one part of the character
sheet and unlocks exactly one next stage. The FSM enforces strict
ordering: you cannot pick skills before assigning stats, cannot
finalize before equipping.

Stages, in order:

    begin -> choose_race -> choose_class -> assign_stats
          -> pick_skills -> equip -> finalize

Demonstrates the sequential-narrowing pattern. Useful for any guided
flow where downstream choices depend on upstream ones (here:
class-specific skill lists, class-specific starter equipment).

Run:

    python examples/chargen.py

Then prompt your MCP client with something like
"build me a stealthy character", or try to skip ahead ("just finalize
my character") and watch the FSM refuse.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "chargen-demo"

# ── reference tables ────────────────────────────────────────────────

_RACES = {
    "human": "Adaptable, +1 to all ability scores.",
    "elf": "Graceful, keen senses, +2 Dexterity.",
    "dwarf": "Resilient, stonecunning, +2 Constitution.",
    "halfling": "Small, lucky, +2 Dexterity, advantage on fear saves.",
}

_CLASSES = {
    "fighter": "Martial all-rounder. Heavy armour, weapon mastery.",
    "wizard": "Arcane spellcaster. Books, INT-based, fragile early on.",
    "rogue": "Skill specialist. Sneak attack, expertise, evasive.",
    "cleric": "Divine spellcaster. Heals, buffs, decent in melee.",
}

_CLASS_SKILLS = {
    "fighter": ["athletics", "intimidation", "perception", "survival"],
    "wizard": ["arcana", "history", "investigation", "insight"],
    "rogue": ["stealth", "sleight_of_hand", "perception", "deception"],
    "cleric": ["medicine", "religion", "insight", "persuasion"],
}

_STARTER_PACKS = {
    "fighter": ["longsword", "shield", "chain mail", "explorer's pack"],
    "wizard": ["quarterstaff", "spellbook", "component pouch", "scholar's pack"],
    "rogue": ["shortsword", "shortbow + 20 arrows", "leather armour", "thieves' tools"],
    "cleric": ["mace", "shield", "scale mail", "holy symbol"],
}

# Point-buy: 27 points across six abilities, each 8..15 before racial bonus.
_ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
_POINT_BUY_TOTAL = 27
_POINT_BUY_MIN = 8
_POINT_BUY_MAX = 15
_POINT_BUY_COSTS = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}


# ── actions ─────────────────────────────────────────────────────────


@action(reads=[], writes=["stage", "name", "log"])
def begin(state: State, name: str) -> State:
    """Open a fresh character sheet under ``name``. First step."""
    return state.update(
        stage="race",
        name=name,
        log=[f"Begin sheet for {name}."],
    )


@action(reads=["stage", "log"], writes=["stage", "race", "log"])
def choose_race(state: State, race: str) -> State:
    """Pick the character's race.

    Args:
        race: One of human, elf, dwarf, halfling.
    """
    race = race.lower()
    if race not in _RACES:
        raise ValueError(f"unknown race {race!r}. Known: {sorted(_RACES)}")
    log = [*state.get("log", []), f"Race: {race} ({_RACES[race]})"]
    return state.update(stage="class", race=race, log=log)


@action(reads=["stage", "log"], writes=["stage", "class_", "log"])
def choose_class(state: State, class_name: str) -> State:
    """Pick the character's class.

    Args:
        class_name: One of fighter, wizard, rogue, cleric.
    """
    class_name = class_name.lower()
    if class_name not in _CLASSES:
        raise ValueError(f"unknown class {class_name!r}. Known: {sorted(_CLASSES)}")
    log = [*state.get("log", []), f"Class: {class_name} ({_CLASSES[class_name]})"]
    return state.update(stage="stats", class_=class_name, log=log)


@action(reads=["stage", "log"], writes=["stage", "stats", "log"])
def assign_stats(
    state: State,
    STR: int,
    DEX: int,
    CON: int,
    INT: int,
    WIS: int,
    CHA: int,
) -> State:
    """Spend 27 points across six abilities (each 8..15 before racial bonus).

    Costs per score: 8=0, 9=1, 10=2, 11=3, 12=4, 13=5, 14=7, 15=9.
    """
    scores = {"STR": STR, "DEX": DEX, "CON": CON, "INT": INT, "WIS": WIS, "CHA": CHA}
    for k, v in scores.items():
        if not (_POINT_BUY_MIN <= v <= _POINT_BUY_MAX):
            raise ValueError(f"{k}={v} out of range [{_POINT_BUY_MIN}, {_POINT_BUY_MAX}]")
    spent = sum(_POINT_BUY_COSTS[v] for v in scores.values())
    if spent != _POINT_BUY_TOTAL:
        raise ValueError(
            f"point-buy must total exactly {_POINT_BUY_TOTAL}; this build spends {spent}"
        )
    log = [*state.get("log", []), f"Stats: {scores}"]
    return state.update(stage="skills", stats=scores, log=log)


@action(reads=["stage", "class_", "log"], writes=["stage", "skills", "log"])
def pick_skills(state: State, skills: list[str]) -> State:
    """Pick exactly two skills from your class list.

    The class list is in ``theodosia://state`` under ``available_skills``
    after class selection. Different classes have different options.
    """
    class_ = state["class_"]
    allowed = set(_CLASS_SKILLS[class_])
    picked = [s.lower() for s in skills]
    if len(picked) != 2:
        raise ValueError("must pick exactly two skills")
    if len(set(picked)) != 2:
        raise ValueError("two skills must be distinct")
    if any(s not in allowed for s in picked):
        raise ValueError(f"{class_} can only choose from {sorted(allowed)}, got {picked}")
    log = [*state.get("log", []), f"Skills: {picked}"]
    return state.update(stage="equip", skills=picked, log=log)


@action(reads=["stage", "class_", "log"], writes=["stage", "equipment", "log"])
def equip(state: State) -> State:
    """Take the starter equipment pack for your class. No choices: each
    class has one canonical kit."""
    class_ = state["class_"]
    pack = list(_STARTER_PACKS[class_])
    log = [*state.get("log", []), f"Equipment: {pack}"]
    return state.update(stage="finalize", equipment=pack, log=log)


@action(reads=["stage", "log"], writes=["stage", "log", "sheet"])
def finalize(state: State) -> State:
    """Finalize the character sheet. Terminal step."""
    sheet = {
        "name": state["name"],
        "race": state["race"],
        "class": state["class_"],
        "stats": state["stats"],
        "skills": state["skills"],
        "equipment": state["equipment"],
    }
    log = [*state.get("log", []), "Sheet finalized."]
    return state.update(stage="done", sheet=sheet, log=log)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            begin=begin,
            choose_race=choose_race,
            choose_class=choose_class,
            assign_stats=assign_stats,
            pick_skills=pick_skills,
            equip=equip,
            finalize=finalize,
        )
        .with_transitions(
            ("begin", "choose_race", Condition.expr("stage == 'race'")),
            ("choose_race", "choose_class", Condition.expr("stage == 'class'")),
            ("choose_class", "assign_stats", Condition.expr("stage == 'stats'")),
            ("assign_stats", "pick_skills", Condition.expr("stage == 'skills'")),
            ("pick_skills", "equip", Condition.expr("stage == 'equip'")),
            ("equip", "finalize", Condition.expr("stage == 'finalize'")),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            stage="begin",
            name="",
            race="",
            class_="",
            stats={},
            skills=[],
            equipment=[],
            sheet={},
            log=[],
        )
        .with_entrypoint("begin")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="chargen",
        instructions=(
            "Guided D&D-flavoured character creation. Six stages run "
            "in strict order: begin, choose_race, choose_class, "
            "assign_stats, pick_skills, equip, finalize. Each stage "
            "writes one part of the character sheet, and the FSM "
            "refuses out-of-order calls. Read theodosia://state for the "
            "in-progress sheet; read theodosia://next to see which step is "
            "valid right now. Useful reference tables (races, classes, "
            "class skill lists, starter packs) live in the action "
            "docstrings."
        ),
    )


if __name__ == "__main__":
    build_server().run()
