"""Two-layer reactive hinting: Theodosia auto-hints + domain hint callback.

After every step (success or refusal), the response carries a ``next_hint``
field. The auto-hint layer is structural and graph-introspective --
derived from valid_next_actions, terminal state, and refusal taxonomy.
The domain layer is an optional callback supplied via ``mount(next_hint=...)``
that can override the auto-hint with richer guidance.

The domain hint wins if it returns a non-empty string. The auto-hint is
the safety net so every response carries some guidance.
"""

from __future__ import annotations

import pytest
from coffee_order import build_server
from fastmcp import Client

from theodosia import ServingMode, mount
from theodosia.adapter import (
    _auto_hint_refusal,
    _auto_hint_success,
    _compose_next_hint,
)

# ── Unit tests on the auto-hint generators directly ────────────────────────


class TestAutoHintSuccess:
    def test_enumerates_reachable_actions(self):
        hint = _auto_hint_success("entry", ["lookup", "shell", "advance_phase"])
        assert hint is not None
        assert "lookup" in hint
        assert "shell" in hint
        assert "advance_phase" in hint

    def test_truncates_long_lists(self):
        many = [f"action_{i}" for i in range(12)]
        hint = _auto_hint_success("entry", many)
        assert hint is not None
        assert "+6 more" in hint  # 12 actions, head=6, tail=6

    def test_terminal_state_message(self):
        hint = _auto_hint_success("conclude", [])
        assert hint is not None
        assert "terminal" in hint.lower()


class TestAutoHintRefusal:
    def test_invalid_transition_cites_reachable(self):
        refusal = {
            "error": "invalid_transition",
            "requested": "shell",
            "valid_next_actions": ["confirm", "cancel"],
        }
        hint = _auto_hint_refusal(refusal)
        assert hint is not None
        assert "'shell'" in hint
        assert "confirm" in hint
        assert "cancel" in hint

    def test_validation_failed_cites_reason(self):
        refusal = {
            "error": "validation_failed",
            "requested": "shell",
            "reason": "command head 'bash' not in allowlist",
            "valid_next_actions": ["lookup", "advance_phase"],
        }
        hint = _auto_hint_refusal(refusal)
        assert hint is not None
        assert "validation" in hint.lower()
        assert "bash" in hint

    def test_action_timeout_cites_seconds(self):
        refusal = {
            "error": "action_timeout",
            "requested": "shell",
            "timeout_seconds": 30,
            "valid_next_actions": ["lookup"],
        }
        hint = _auto_hint_refusal(refusal)
        assert hint is not None
        assert "30" in hint
        assert "timed out" in hint.lower()

    def test_action_error_cites_exception(self):
        refusal = {
            "error": "action_error",
            "requested": "shell",
            "error_type": "ConnectionRefusedError",
            "error_message": "host unreachable",
            "valid_next_actions": ["lookup"],
        }
        hint = _auto_hint_refusal(refusal)
        assert hint is not None
        assert "ConnectionRefusedError" in hint


class TestCompose:
    def test_no_callback_falls_back_to_auto_hint(self):
        hint = _compose_next_hint(
            state={},
            valid_next=["a", "b"],
            last_action="entry",
            refusal=None,
            domain_callback=None,
        )
        assert hint is not None
        assert "a" in hint and "b" in hint

    def test_domain_hint_wins_when_non_empty(self):
        def cb(state, valid_next, last_action, refusal):
            return "domain-specific guidance"

        hint = _compose_next_hint(
            state={},
            valid_next=["a", "b"],
            last_action="entry",
            refusal=None,
            domain_callback=cb,
        )
        assert hint == "domain-specific guidance"

    def test_domain_hint_none_falls_through_to_auto(self):
        def cb(state, valid_next, last_action, refusal):
            return None

        hint = _compose_next_hint(
            state={},
            valid_next=["a", "b"],
            last_action="entry",
            refusal=None,
            domain_callback=cb,
        )
        assert hint is not None
        assert "a" in hint  # auto-hint kicked in

    def test_three_arg_callback_backwards_compatible(self):
        # Older callbacks predate the refusal arg.
        def cb(state, valid_next, last_action):
            return f"three-arg hint for {last_action}"

        hint = _compose_next_hint(
            state={},
            valid_next=["a"],
            last_action="entry",
            refusal=None,
            domain_callback=cb,
        )
        assert hint == "three-arg hint for entry"

    def test_callback_exception_falls_back_to_auto(self):
        def cb(state, valid_next, last_action, refusal):
            raise RuntimeError("oops")

        hint = _compose_next_hint(
            state={},
            valid_next=["a"],
            last_action="entry",
            refusal=None,
            domain_callback=cb,
        )
        # callback crashed → auto-hint fires (graceful degradation)
        assert hint is not None
        assert "a" in hint


# ── Integration: mount with a callback, drive via Client, see the hint ─────


@pytest.mark.asyncio
async def test_step_response_carries_auto_hint_on_success():
    """No callback supplied -- every step response still has an auto-hint."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out = r.structured_content
        assert "next_hint" in out
        hint = out["next_hint"]
        # Auto-hint enumerates reachable actions.
        assert "pay" in hint
        assert "add_modifier" in hint
        assert "cancel" in hint


@pytest.mark.asyncio
async def test_step_response_carries_auto_hint_on_refusal():
    """Invalid transitions surface a structural hint with reachable actions."""
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # `pay` isn't reachable from the entrypoint state -- must take_order first.
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"
        assert "next_hint" in out
        # The auto-hint says: not reachable, here's what is.
        hint = out["next_hint"]
        assert "'pay'" in hint or "pay" in hint
        assert "take_order" in hint  # the actual reachable action


@pytest.mark.asyncio
async def test_domain_hint_override():
    """A registered callback's return value replaces the auto-hint."""
    from coffee_order import build_application

    def domain_hint(state, valid_next, last_action, refusal=None):
        if last_action == "take_order":
            return "Domain says: now pay before fulfilling."
        return None

    server = mount(build_application, mode=ServingMode.STEP, next_hint=domain_hint)
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out = r.structured_content
        assert out["next_hint"] == "Domain says: now pay before fulfilling."


@pytest.mark.asyncio
async def test_domain_hint_falls_back_to_auto_when_none():
    """Callback returning None lets the auto-hint fire."""
    from coffee_order import build_application

    def domain_hint(state, valid_next, last_action, refusal=None):
        return None  # always defer

    server = mount(build_application, mode=ServingMode.STEP, next_hint=domain_hint)
    async with Client(server) as client:
        r = await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 2}}
        )
        out = r.structured_content
        # Auto-hint should be there since domain returned None.
        assert "next_hint" in out
        assert "pay" in out["next_hint"]


@pytest.mark.asyncio
async def test_domain_callback_sees_refusal():
    """On refusal, the domain callback receives the refusal payload."""
    from coffee_order import build_application

    captured: list[dict | None] = []

    def domain_hint(state, valid_next, last_action, refusal=None):
        captured.append(refusal)
        if refusal:
            return f"Refused: {refusal.get('error')}"
        return None

    server = mount(build_application, mode=ServingMode.STEP, next_hint=domain_hint)
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        out = r.structured_content
        assert out["error"] == "invalid_transition"
        # Domain hint saw the refusal and emitted its own message.
        assert out["next_hint"] == "Refused: invalid_transition"
        # The callback was passed the refusal dict.
        assert any(r and r.get("error") == "invalid_transition" for r in captured)
