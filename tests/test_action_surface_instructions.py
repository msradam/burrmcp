"""mount() appends a synthesized action listing to the server's
``instructions`` so caller LLMs see the action surface at connect time
without first reading ``burr://graph``.

The synthesis covers:
* every action by name + first-line docstring
* every transition, with `(when: <expr>)` for conditional edges
* the entrypoint, prefixed on the actions heading
The user's own ``instructions`` (if any) is preserved verbatim and
placed first.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from burrmcp import ServingMode, mount


@action(reads=[], writes=["status"])
def start(state: State) -> State:
    """Open the run."""
    return state.update(status="started")


@action(reads=["status"], writes=["count"])
def tick(state: State) -> State:
    """Increment a counter step."""
    return state.update(count=state.get("count", 0) + 1)


@action(reads=["count"], writes=["status"])
def finalize(state: State) -> State:
    """Terminal step.

    Multi-line docstring; only the first line should land in
    instructions to keep the surface compact.
    """
    return state.update(status="finalized")


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(start=start, tick=tick, finalize=finalize)
        .with_transitions(
            ("start", "tick"),
            ("tick", "tick", Condition.expr("status == 'started'")),
            ("tick", "finalize", Condition.expr("status == 'started'")),
        )
        .with_state(status="initial", count=0)
        .with_entrypoint("start")
        .build()
    )


def _server_instructions(server) -> str:
    """Pull the instructions string from a mounted FastMCP server."""
    instr = getattr(server, "instructions", None)
    if instr is None and hasattr(server, "_mcp_server"):
        instr = getattr(server._mcp_server, "instructions", None)
    assert instr is not None, "FastMCP exposes instructions"
    return instr


def test_instructions_list_actions_with_first_line_docstring():
    server = mount(_factory, mode=ServingMode.STEP, name="surface-test")
    instr = _server_instructions(server)
    assert "Actions (entry: start):" in instr
    assert "- start: Open the run." in instr
    assert "- tick: Increment a counter step." in instr
    assert "- finalize: Terminal step." in instr
    # Multi-line docstrings collapse to first line only.
    assert "Multi-line docstring" not in instr


def test_instructions_list_transitions_with_conditions():
    server = mount(_factory, mode=ServingMode.STEP, name="surface-trans")
    instr = _server_instructions(server)
    assert "Transitions:" in instr
    assert "- start -> tick" in instr
    assert "- tick -> tick" in instr
    assert "- tick -> finalize" in instr
    assert "(when: status == 'started')" in instr


def test_user_instructions_appear_before_action_listing():
    user_text = "USER-INSTRUCTIONS-MARKER: this is what the caller wrote."
    server = mount(
        _factory,
        mode=ServingMode.STEP,
        name="surface-user",
        instructions=user_text,
    )
    instr = _server_instructions(server)
    user_idx = instr.find(user_text)
    surface_idx = instr.find("Actions (entry: start):")
    assert user_idx != -1
    assert surface_idx != -1
    assert user_idx < surface_idx


def test_instructions_without_user_string_still_emit_action_surface():
    server = mount(_factory, mode=ServingMode.STEP, name="surface-no-user")
    instr = _server_instructions(server)
    assert "Actions (entry: start):" in instr
    # Discovery hint also present.
    assert "burr://graph" in instr


def test_unconditional_transitions_omit_when_clause():
    server = mount(_factory, mode=ServingMode.STEP, name="surface-uncond")
    instr = _server_instructions(server)
    # start -> tick has no condition.
    for line in instr.splitlines():
        if "- start -> tick" in line:
            assert "(when:" not in line, line
