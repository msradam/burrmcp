"""Reactive next_hint: the steering string appended after every step.

After every step (success or refusal), Theodosia appends a single
``next_hint`` string to the response. The hint is generated in two
layers:

1. **Auto-hint** (this module): derived from graph introspection alone --
   what transitions are reachable, what kind of refusal just happened,
   whether the session is terminal. No domain knowledge required; works
   for any Burr graph mounted via ``mount()``.

2. **Domain hint** (caller-supplied ``next_hint`` callback): receives
   the same structural signals plus the refusal payload, and can return
   a domain-specific override. When provided and non-None, it replaces
   the auto-hint; otherwise the auto-hint is used.

The split mirrors POSIX: errno (structural taxonomy) lives at the kernel
layer; strerror (semantic translation) lives at the libc layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _auto_hint_success(action: str, valid_next: list[str]) -> str | None:
    """Structural hint after a successful step.

    Terminal nodes get a "session terminal" signal. Otherwise an
    enumeration of reachable actions. Both are derivable from the Burr
    graph alone -- no domain knowledge is consumed.
    """
    if not valid_next:
        return "Session is at a terminal state. No further actions are reachable."
    head = ", ".join(valid_next[:6])
    more = f" (+{len(valid_next) - 6} more)" if len(valid_next) > 6 else ""
    return f"Reachable actions from current state: {head}{more}."


def _auto_hint_refusal(refusal: dict[str, Any]) -> str | None:
    """Structural hint after a refusal.

    Maps the Theodosia refusal taxonomy (unknown_action / invalid_transition /
    validation_failed / action_timeout / action_error) to short,
    model-readable strings that cite the structural reason without
    claiming to know the domain.
    """
    kind = refusal.get("error")
    requested = refusal.get("requested") or "?"
    valid = refusal.get("valid_next_actions") or []
    head = ", ".join(valid[:6]) if valid else "(none)"
    more = f" (+{len(valid) - 6} more)" if len(valid) > 6 else ""

    if kind == "unknown_action":
        return f"{requested!r} is not an action. Reachable now: {head}{more}."
    if kind == "invalid_transition":
        return (
            f"Action {requested!r} is not reachable from the current state. "
            f"Reachable now: {head}{more}."
        )
    if kind == "validation_failed":
        reason = refusal.get("reason") or "(unspecified)"
        return (
            f"Action {requested!r} failed input validation: {reason}. "
            f"Check the action's required inputs, then retry -- or pick a "
            f"different reachable action: {head}{more}."
        )
    if kind == "action_timeout":
        timeout_s = refusal.get("timeout_seconds")
        return (
            f"Action {requested!r} timed out after {timeout_s}s without "
            f"advancing state. Try a narrower-scoped probe, or a different "
            f"reachable action: {head}{more}."
        )
    if kind == "action_error":
        err_type = refusal.get("error_type") or "Exception"
        err_msg = _truncate_words(refusal.get("error_message") or "", 160)
        return (
            f"Action {requested!r} raised {err_type}: {err_msg}. "
            f"Vary the inputs, or pick a different reachable action: "
            f"{head}{more}."
        )
    return None


def _truncate_words(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars, on a word boundary, with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rsplit(None, 1)[0] if " " in head else head
    return f"{cut.rstrip(',.;:!?')}..."


def _compose_next_hint(
    *,
    state: dict[str, Any],
    valid_next: list[str],
    last_action: str,
    refusal: dict[str, Any] | None,
    domain_callback: Callable[..., str | None] | None,
) -> str | None:
    """Run the domain callback first; fall back to the auto-hint.

    ``domain_callback`` is the user-supplied ``next_hint`` from ``mount()``.
    Callable accepts ``(state, valid_next, last_action, refusal=None)``;
    for backwards compatibility with three-arg callbacks, the refusal
    arg is omitted when the inspected signature can't accept it. Domain
    hint wins iff it returns a non-empty string; otherwise the
    structural auto-hint is used.
    """
    if domain_callback is not None:
        domain_hint: str | None = None
        try:
            try:
                # Try the four-arg form first.
                domain_hint = domain_callback(state, valid_next, last_action, refusal)
            except TypeError:
                # Older callbacks predate the refusal arg.
                domain_hint = domain_callback(state, valid_next, last_action)
        except Exception:
            domain_hint = None
        if domain_hint:
            return domain_hint
    if refusal is None:
        return _auto_hint_success(last_action, valid_next)
    return _auto_hint_refusal(refusal)
