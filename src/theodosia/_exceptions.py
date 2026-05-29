"""Exception types raised inside ``_step_application`` and translated into
structured MCP refusals at the wire boundary.

Re-exported from :mod:`theodosia.adapter` for backwards compatibility with
downstream code that imports from there directly. New code can import
either from ``theodosia.adapter`` or from this module.
"""

from __future__ import annotations

from typing import Any


class InvalidTransitionError(Exception):
    """Raised when a client requests an action that isn't reachable now.

    Carries the list of currently valid action names so the client can
    recover without re-fetching ``theodosia://next``.
    """

    def __init__(self, requested: str, valid: list[str]) -> None:
        self.requested = requested
        self.valid = valid
        msg = (
            f"action {requested!r} is not reachable from current state. "
            f"Valid actions now: {valid or '(none, terminal)'}."
        )
        super().__init__(msg)


class ActionExecutionError(Exception):
    """Raised when an action's wrapped function raises during execution.

    Wraps the original exception so callers can record a structured
    refusal entry (with the underlying error message) in the session's
    history. ``original`` is the wrapped exception for callers that
    want the traceback or the exact type.
    """

    def __init__(self, action_name: str, original: BaseException) -> None:
        self.action_name = action_name
        self.original = original
        super().__init__(f"action {action_name!r} raised {type(original).__name__}: {original}")


class ActionTimeoutError(Exception):
    """Raised when an action exceeds its allotted execution time.

    The action's coroutine is cancelled via ``asyncio.wait``; for async
    actions doing I/O, cancellation is prompt. For sync or CPU-bound
    actions, cancellation is best-effort and the underlying work may
    continue running in a worker thread until it yields. The session's
    FSM does not advance regardless.
    """

    def __init__(self, action_name: str, timeout_seconds: float) -> None:
        self.action_name = action_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"action {action_name!r} exceeded the {timeout_seconds}s timeout")


class ValidationFailed(Exception):
    """Raised by an input validator to refuse a call before execution.

    Validators run between MCP wire arrival and action execution. They
    receive the current public state and the inputs the client sent;
    they may raise this to refuse, return a dict to substitute normalised
    inputs, or return None to accept the originals.

    The handler catches ``ValidationFailed``, returns a structured
    ``{"error": "validation_failed", "reason": ..., "details": ...}`` to
    the client, and records a refusal in ``theodosia://history`` with
    ``refusal_reason: "validation_failed"``. The FSM does not advance.

    Use ``details`` to attach structured per-field information (e.g.
    Pydantic validation errors) without baking it into the reason string.
    """

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)
