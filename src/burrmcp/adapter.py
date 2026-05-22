"""Mount a Burr Application as a FastMCP server.

One serving mode:

  • ``ServingMode.STEP``:    One ``step(action_name, **inputs)``
                             meta-tool. Server enforces valid
                             transitions. The four-tool surface
                             (step + reset_session + fork_at +
                             fork_from_past) is constant across every
                             FSM, regardless of action count, so the
                             tool listing stays compact while the
                             action namespace lives in step's argument
                             schema and ``burr://graph``.

The mount registers eight resources:

  • ``burr://graph``:           static description of the FSM topology
                                (actions, reads/writes/inputs, edges
                                with conditions). Read once per session.
  • ``burr://state``:           current Application state as JSON.
  • ``burr://next``:            actions reachable from current state.
  • ``burr://history``:         per-session timeline of every action
                                attempt (successes + refusals).
  • ``burr://trace``:           Burr's on-disk LocalTrackingClient log.
  • ``burr://session``:         tracker coordinates (project, app_id,
                                app_dir, partition_key) for locating
                                this session's data on disk.
  • ``burr://subruns``:         index of sub-Application runs spawned
                                in this session via ``spawn_subapp``.
  • ``burr://subruns/{id}``:    full record for one sub-run.

Per-session isolation:

  Pass a callable factory ``() -> Application`` instead of an
  ``Application`` instance. Each MCP session then gets its own
  Application built lazily on first tool call and stored in FastMCP's
  session state. Two clients connected to the same server see
  independent state.

  Passing an Application instance directly preserves the simpler
  shared-state behavior, where all sessions mutate one FSM.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import re
import time
import typing
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pydantic
from burr.core import Application
from burr.core.action import Action, Condition
from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

ApplicationFactory = Callable[[], Application]
ApplicationOrFactory = Application | ApplicationFactory

# Defaults for session-store eviction.
_DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour idle
_DEFAULT_MAX_SESSIONS = 100


class ServingMode(str, Enum):  # noqa: UP042  # (str, Enum) for stable wire serialization
    STEP = "step"
    # ``TOOLS`` (one MCP tool per @action, no enforcement) and ``DYNAMIC``
    # (per-session ``tools/list_changed`` visibility) were carved out into
    # ``burrmcp._experimental.modes`` after STEP became the sole product.
    # The enum is preserved (single-member) so callers that pass
    # ``mode=ServingMode.STEP`` keep working.


# ── step tool response schema ────────────────────────────────────────
# Models declare the contract clients see in ``step``'s output schema.
# Body of ``step`` returns plain dicts shaped to match these; the union
# is registered with FastMCP as a JSON Schema, not used at runtime to
# validate (so the existing dict-returning code stays unchanged).


class _StepSuccess(pydantic.BaseModel):
    """Successful step: action ran, state advanced."""

    action: str
    result: dict[str, Any] | None = None
    state: dict[str, Any]
    valid_next_actions: list[str]
    app_id: str
    tracker_project: str | None = None
    streamed: bool | None = None
    chunks: int | None = None


class _StepUnknownAction(pydantic.BaseModel):
    """Refusal: requested action name is not in the FSM."""

    error: typing.Literal["unknown_action"]
    requested: str
    known_actions: list[str]


class _StepInvalidTransition(pydantic.BaseModel):
    """Refusal: action exists but is not reachable from current state."""

    error: typing.Literal["invalid_transition"]
    requested: str
    valid_next_actions: list[str]
    message: str


class _StepValidationFailed(pydantic.BaseModel):
    """Refusal: input validation rejected the call before dispatch."""

    error: typing.Literal["validation_failed"]
    requested: str
    reason: str
    details: dict[str, Any] | None = None
    valid_next_actions: list[str]


class _StepActionTimeout(pydantic.BaseModel):
    """Refusal: action exceeded its timeout budget."""

    error: typing.Literal["action_timeout"]
    requested: str
    timeout_seconds: float
    message: str
    valid_next_actions: list[str]


class _StepActionError(pydantic.BaseModel):
    """Refusal: action body raised an exception."""

    error: typing.Literal["action_error"]
    requested: str
    error_type: str
    error_message: str
    valid_next_actions: list[str]


def _step_response_schema() -> dict[str, Any]:
    """JSON Schema for the ``step`` tool's response.

    MCP requires the output schema to be a single ``type: "object"`` at
    the top level, not a union. We emit a merged object whose ``error``
    field discriminates: when ``error`` is absent the response carries
    the ``_StepSuccess`` fields (action, result, state, ...); when
    ``error`` is present it carries one of the refusal shapes
    enumerated in ``error``'s allowed values. Per-shape required-field
    constraints are documented in ``description`` rather than enforced
    via ``oneOf``; clients should branch on ``error`` to interpret the
    rest of the payload.
    """
    return {
        "type": "object",
        "description": (
            "Result of one step. When `error` is absent, the response is a "
            "successful step (action, result, state, valid_next_actions, "
            "app_id, tracker_project, optionally streamed/chunks). When "
            "`error` is set, the response is a structured refusal; check "
            "`error` to interpret the rest:\n"
            "  - unknown_action: requested + known_actions\n"
            "  - invalid_transition: requested + valid_next_actions + message\n"
            "  - validation_failed: requested + reason + details + valid_next_actions\n"
            "  - action_timeout: requested + timeout_seconds + message + valid_next_actions\n"
            "  - action_error: requested + error_type + error_message + valid_next_actions"
        ),
        "properties": {
            # Success-side fields.
            "action": {"type": "string", "description": "Name of the action that ran."},
            "result": {
                "type": ["object", "null"],
                "additionalProperties": True,
                "description": "Action's structured return value.",
            },
            "state": {
                "type": "object",
                "additionalProperties": True,
                "description": "Public Application state after the step.",
            },
            "app_id": {"type": "string", "description": "Application uid."},
            "tracker_project": {
                "type": ["string", "null"],
                "description": "LocalTrackingClient project name if attached.",
            },
            "streamed": {
                "type": ["boolean", "null"],
                "description": "True when the action was a streaming action.",
            },
            "chunks": {
                "type": ["integer", "null"],
                "description": "Streamed chunk count (when streamed is true).",
            },
            # Refusal-side fields.
            "error": {
                "type": "string",
                "enum": [
                    "unknown_action",
                    "invalid_transition",
                    "validation_failed",
                    "action_timeout",
                    "action_error",
                ],
                "description": (
                    "Refusal discriminator. When set, the response carries the "
                    "matching refusal payload's fields."
                ),
            },
            "requested": {
                "type": "string",
                "description": "Name the client passed; present on every refusal.",
            },
            "known_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All action names in the FSM (unknown_action only).",
            },
            "valid_next_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Actions reachable from the current state. Present on "
                    "success and on every refusal so the agent can self-correct."
                ),
            },
            "message": {
                "type": "string",
                "description": "Human-readable message (invalid_transition, action_timeout).",
            },
            "reason": {
                "type": "string",
                "description": "Validation failure reason (validation_failed only).",
            },
            "details": {
                "type": ["object", "null"],
                "additionalProperties": True,
                "description": "Validation failure details (validation_failed only).",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Configured timeout (action_timeout only).",
            },
            "error_type": {
                "type": "string",
                "description": (
                    "Exception class name of the underlying error (action_error only)."
                ),
            },
            "error_message": {
                "type": "string",
                "description": "Stringified exception (action_error only).",
            },
        },
        "additionalProperties": True,
    }


# State keys Burr writes itself. Hide them from the public state view so
# the MCP client sees only the user's domain fields.
_INTERNAL_STATE_KEYS = frozenset({"__SEQUENCE_ID", "__PRIOR_STEP"})


def _public_state(state_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state_dict.items() if k not in _INTERNAL_STATE_KEYS}


def _serializable_state(
    state_dict: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a JSON-serialisable copy of ``state_dict`` plus a list of
    keys whose values had to be stringified.

    Burr lets actions put anything in state (numpy arrays, connection
    objects, Pydantic models, callables). The wire format is JSON, so
    we test each top-level value with strict ``json.dumps`` and fall
    back to ``str(value)`` for anything that fails. The list of coerced
    keys is surfaced to the client via ``_burrmcp.coerced_keys`` on
    the state resource so it knows the round-trip is lossy and can
    flag downstream.

    Nested structures inside a successfully-serialised value are not
    re-checked: ``json.dumps`` already vetted them as a whole.
    """
    out: dict[str, Any] = {}
    coerced: list[str] = []
    for key, value in state_dict.items():
        try:
            json.dumps(value)
            out[key] = value
        except (TypeError, ValueError):
            out[key] = str(value)
            coerced.append(key)
    return out, coerced


_TRACE_MAX_ENTRIES = 1000  # cap burr://trace response to the last N records

# Function attribute set by ``burrmcp.importing`` (and any other code
# that wants to annotate an action with a per-call timeout). ``mount``
# reads this off each action's ``fn`` and uses it in preference to the
# server-wide default.
_PER_ACTION_TIMEOUT_ATTR = "_burrmcp_timeout_seconds"


def _action_timeout(action: Action, server_default: float | None) -> float | None:
    """Return the timeout to use for ``action``.

    Per-action override (set by ``ToolSpec.timeout_seconds`` via the
    importer, or by hand-tagging a function with
    ``fn._burrmcp_timeout_seconds = N``) wins over the server-wide
    default. ``None`` at either level disables timeout for that level;
    a numeric override on the action applies even when the server
    default is ``None``.
    """
    fn = getattr(action, "fn", None)
    per_action = getattr(fn, _PER_ACTION_TIMEOUT_ATTR, None) if fn is not None else None
    if per_action is not None:
        return float(per_action)
    return server_default


def _tracker_project(app: Application) -> str | None:
    """Return the LocalTrackingClient project name, or None.

    Surfaced on every step/fork meta-tool response so even collapsed
    tool-result views in MCP clients carry enough to locate the
    session's data on disk (``~/.burr/<project>/<app_id>/``).
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    return tracker.project_id


def _restore_snapshot(
    *,
    entry: Any,
    factory: ApplicationFactory,
    state_dict: dict[str, Any],
    last_action: str | None,
    sequence_id_override: int | None = None,
    kept_subruns: dict[str, Any] | None = None,
) -> tuple[Application, dict[str, Any], list[str]]:
    """Shared body for ``fork_at`` and ``fork_from_past``.

    Rebuilds the session's Application via the factory, overwrites its
    state with ``state_dict`` plus ``__PRIOR_STEP`` (and optionally
    ``__SEQUENCE_ID`` for in-session forks where the tracker count must
    stay monotonic), and applies the caller's sub-runs policy. Returns
    ``(new_app, serialized_state, valid_next_actions)`` for the caller
    to build its response payload around.

    ``kept_subruns`` semantics:
      * ``None`` (default): clear all (``fork_from_past`` behavior).
      * dict: replace the session's subruns with this filtered subset
        (``fork_at`` keeps sub-runs spawned before the fork point).
    """
    from burr.core.state import State as _BurrState

    entry.application = factory()
    new_app = entry.application
    assert new_app is not None

    forked_state_dict: dict[str, Any] = {
        **state_dict,
        "__PRIOR_STEP": last_action,
    }
    if sequence_id_override is not None:
        forked_state_dict["__SEQUENCE_ID"] = sequence_id_override
    new_app.update_state(_BurrState(forked_state_dict))

    entry.subruns = kept_subruns if kept_subruns is not None else {}

    new_state, coerced = _serializable_state(_public_state(new_app.state.get_all()))
    if coerced:
        new_state["_burrmcp"] = {"coerced_keys": coerced}
    valid_next = valid_next_action_names(new_app)
    entry.last_access = time.monotonic()

    return new_app, new_state, valid_next


def _tracker_log_path(app: Application) -> Path | None:
    """Locate the on-disk log file for this Application's Burr tracker.

    Reads ``app._tracker`` which is Burr's internal slot for the
    ``LocalTrackingClient``. We pin Burr to a minor version range
    because of this and similar internals (see ``pyproject.toml``).
    Returns ``None`` when the Application has no tracker, or has a
    non-local one, or the resolved path is outside the tracker's
    own storage directory.
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    try:
        storage_dir = Path(tracker.storage_dir).expanduser().resolve()
        log_path = (storage_dir / app.uid / LocalTrackingClient.LOG_FILENAME).resolve()
    except (OSError, AttributeError):
        return None
    # Defence in depth: the computed log path must sit under the tracker's
    # storage dir. If app.uid contained a traversal sequence (it shouldn't,
    # Burr generates UUIDs, but belt-and-braces), refuse to read it.
    try:
        log_path.relative_to(storage_dir)
    except ValueError:
        return None
    return log_path


def _read_trace(path: Path, *, tail: int = _TRACE_MAX_ENTRIES) -> list[dict]:
    """Read a JSONL trace file and return the last ``tail`` records.

    Malformed lines are skipped silently rather than tanking the whole
    response. The cap is in place because Burr's tracker is append-only;
    long-running sessions accumulate; an MCP client doesn't want the
    full 50 MB log returned over the wire on every read.
    """
    entries: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if tail and len(entries) > tail:
        entries = entries[-tail:]
    return entries


def _render_action_surface(app: Application) -> str:
    """Render a compact text summary of the FSM's action + transition surface.

    Appended to the server's `instructions` so an MCP client sees the
    action namespace at connect time, before reading any resources. The
    first line of each action's docstring (if any) becomes its summary;
    transitions show source and target, plus a `(when: expr)` clause for
    conditional edges. Inputs are deliberately omitted; they live on the
    `step` tool's argument schema (or `burr://graph` for full detail).
    """
    lines: list[str] = []
    entry = getattr(app, "entrypoint", None)
    if entry:
        lines.append(f"Actions (entry: {entry}):")
    else:
        lines.append("Actions:")
    for a in app.graph.actions:
        fn = getattr(a, "fn", None)
        doc = (fn.__doc__ or "").strip() if fn is not None and fn.__doc__ else ""
        first = doc.splitlines()[0] if doc else ""
        if first:
            lines.append(f"  - {a.name}: {first}")
        else:
            lines.append(f"  - {a.name}")

    lines.append("")
    lines.append("Transitions:")
    for t in app.graph.transitions:
        cond = getattr(t.condition, "_name", None) or getattr(t.condition, "name", None)
        if cond and cond != "default":
            lines.append(f"  - {t.from_.name} -> {t.to.name}  (when: {cond})")
        else:
            lines.append(f"  - {t.from_.name} -> {t.to.name}")
    return "\n".join(lines)


def _compute_graph_summary(app: Application, server_name: str) -> dict[str, Any]:
    """Build a static description of an Application's graph.

    Computed once at mount time and returned as-is by the
    ``burr://graph`` resource. Includes per-action metadata
    (description, reads, writes, required/optional inputs) and the
    full transition table including conditions as printed expressions.

    The point of this surface is cold-start discovery: a model
    connecting to the server can read one resource and have the full
    topology without trial-and-error or repeated state probes.
    """
    actions_meta: list[dict[str, Any]] = []
    for a in app.graph.actions:
        required, optional = _action_inputs(a)
        fn = getattr(a, "fn", None)
        doc = (fn.__doc__ or "").strip() if fn is not None and fn.__doc__ else ""
        actions_meta.append(
            {
                "name": a.name,
                "description": doc,
                "reads": list(a.reads or []),
                "writes": list(a.writes or []),
                "required_inputs": required,
                "optional_inputs": optional,
            }
        )

    transitions_meta: list[dict[str, Any]] = []
    for t in app.graph.transitions:
        cond_expr: str | None = None
        try:
            cond = t.condition
            cond_name = getattr(cond, "_name", None) or getattr(cond, "name", None)
            # Burr's Condition.expr produces a condition whose `name`
            # is the printed expression, which is exactly what a model
            # needs to know when to take the edge. ``default`` means
            # unconditional.
            if cond_name and cond_name != "default":
                cond_expr = cond_name
        except Exception:
            cond_expr = None
        transitions_meta.append(
            {
                "from": t.from_.name,
                "to": t.to.name,
                "condition": cond_expr,
            }
        )

    # Optionally surface the Pydantic JSON schema for state if the
    # user wired up Burr's PydanticTypingSystem. Untyped state shows
    # up as None here; consumers fall back to inferring shape from
    # per-action ``reads``/``writes``.
    state_schema: dict[str, Any] | None = None
    try:
        ts = app.state.typing_system
        state_type = ts.state_type() if hasattr(ts, "state_type") else None
        if state_type is not None and hasattr(state_type, "model_json_schema"):
            state_schema = state_type.model_json_schema()
    except Exception:
        state_schema = None

    return {
        "name": server_name,
        "entrypoint": app.entrypoint,
        "actions": actions_meta,
        "transitions": transitions_meta,
        "state_schema": state_schema,
        "meta_tools": [
            {
                "name": "reset_session",
                "description": (
                    "Reset this session's FSM to its entrypoint, clearing "
                    "sub-runs and appending a reset marker to history. "
                    "Always callable regardless of FSM state. Refuses in "
                    "shared-app mode."
                ),
            },
            {
                "name": "fork_at",
                "description": (
                    "Rewind the session to the state captured after a "
                    "specific history entry (by ``seq`` from "
                    "``burr://history``). Lets an agent explore alternate "
                    "paths from any checkpoint without losing the audit "
                    "trail. Refuses in shared-app mode."
                ),
            },
            {
                "name": "fork_from_past",
                "description": (
                    "Resume a past Burr run by loading its state from "
                    "disk. Requires the Application to have a "
                    "LocalTrackingClient. Use for resuming sessions "
                    "across server restarts or forking from any "
                    "persisted past app_id."
                ),
            },
        ],
    }


def valid_next_action_names(app: Application) -> list[str]:
    """Names of actions reachable from the current state.

    For a non-branching graph this is a list of one. For a branching
    graph, all actions whose outgoing-from-prior-step condition
    evaluates true are returned. After a terminal action with no
    outgoing transitions, returns an empty list.
    """
    prior = app.state.get("__PRIOR_STEP")
    if prior is None:
        return [app.entrypoint]
    valid: list[str] = []
    for t in app.graph.transitions:
        if t.from_.name != prior:
            continue
        try:
            if t.condition.run(app.state)[Condition.KEY]:
                valid.append(t.to.name)
        except Exception:
            # Condition that depends on state keys not yet set is
            # treated as not-reachable, same as Burr's own behavior.
            continue
    return valid


def _action_inputs(action: Action) -> tuple[list[str], list[str]]:
    """Return ``(required, optional)`` input names for an action."""
    raw = action.inputs
    if isinstance(raw, tuple) and len(raw) == 2:
        req, opt = raw
        return list(req), list(opt)
    return list(raw or []), []


def _action_signature_params(action: Action) -> list[inspect.Parameter]:
    """Synthesise a Parameter list for an action's MCP tool signature.

    Burr action functions take ``state`` plus the declared inputs.
    The MCP tool sees only the inputs; ``state`` is supplied internally.
    Default values and type annotations come from the wrapped function
    where available; otherwise inputs default to ``str``.

    Resolves ``from __future__ import annotations`` postponed evaluation
    via ``typing.get_type_hints`` so the annotations are real types
    rather than strings, which FastMCP's pydantic-based schema generation
    requires.
    """
    required, optional = _action_inputs(action)
    fn = getattr(action, "fn", None)
    sig: inspect.Signature | None = None
    resolved_hints: dict[str, Any] = {}
    if fn is not None:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        try:
            resolved_hints = typing.get_type_hints(fn)
        except Exception:
            resolved_hints = {}

    def _annotation_for(name: str) -> Any:
        if name in resolved_hints:
            return resolved_hints[name]
        if sig and name in sig.parameters:
            p = sig.parameters[name]
            if p.annotation is not inspect.Parameter.empty:
                return p.annotation
        return str

    params: list[inspect.Parameter] = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=_annotation_for(name),
        )
        for name in required
    ]
    for name in optional:
        default: Any = None
        if sig and name in sig.parameters:
            p = sig.parameters[name]
            if p.default is not inspect.Parameter.empty:
                default = p.default
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=_annotation_for(name),
            )
        )
    return params


class InvalidTransitionError(Exception):
    """Raised when a client requests an action that isn't reachable now.

    Carries the list of currently valid action names so the client can
    recover without re-fetching ``burr://next``.
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

    The action's coroutine is cancelled via ``asyncio.wait_for``; for
    async actions doing I/O, cancellation is prompt. For sync or
    CPU-bound actions, cancellation is best-effort and the underlying
    work may continue running on its event-loop slot until it yields.
    The session's FSM does not advance.
    """

    def __init__(self, action_name: str, timeout_seconds: float) -> None:
        self.action_name = action_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"action {action_name!r} exceeded the {timeout_seconds}s timeout")


#: ContextVar set by the step handler around each ``_step_application``
#: call. Reads inside an action body see their session's entry; used by
#: ``spawn_subapp`` to record sub-run timelines on the parent session.
_current_session_entry: contextvars.ContextVar[_SessionEntry | None] = contextvars.ContextVar(
    "_burrmcp_current_session", default=None
)

#: ContextVar set by the step handler around each ``_step_application``
#: call. Holds the FastMCP ``Context`` injected by the MCP transport so
#: action bodies can call ``ctx.sample(...)``, ``ctx.elicit(...)``,
#: ``ctx.report_progress(...)``, ``ctx.read_resource(...)``, etc.
#: Reads via the public ``current_mcp_context()`` helper return the
#: current value (or ``None`` outside an action body).
_current_fastmcp_context: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "_burrmcp_current_fastmcp_context", default=None
)


def current_mcp_context() -> Context | None:
    """Return the FastMCP ``Context`` for the currently-dispatching tool call.

    Returns ``None`` outside an action body. Inside an action driven via
    burrmcp's adapter, returns the Context FastMCP injected into the
    step handler, so action bodies can call ``ctx.sample(...)``,
    ``ctx.elicit(...)``, ``ctx.report_progress(...)``,
    ``ctx.read_resource(...)``, etc.

    Use this to delegate LLM work to the connected agent's model, ask
    the user for interactive confirmation, or read another resource the
    server exposes from inside an action body.
    """
    return _current_fastmcp_context.get()


async def spawn_subapp(
    sub_application: Application,
    *,
    label: str | None = None,
    halt_after: list[str] | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a sub-Application inside an action and record its timeline.

    The sub-run is appended to the parent session's subruns dict and
    surfaced via ``burr://subruns`` and ``burr://subruns/{subrun_id}``.

    Args:
        sub_application: Built ``burr.core.Application`` to run. Each
            call gets a fresh subrun_id.
        label: Friendly name attached to the subrun record.
        halt_after: Forwarded to ``app.arun(halt_after=...)``. Defaults
            to the sub-graph's last action.
        inputs: Forwarded to ``app.arun(inputs=...)``.

    Returns:
        ``{"subrun_id": str, "final_state": dict, "label": str | None}``.

    Outside an active session the sub-run still executes but isn't
    recorded; the function returns the final state regardless.
    """
    entry = _current_session_entry.get()
    sub_id = f"sub-{uuid.uuid4()}"
    started = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "id": sub_id,
        "label": label,
        "started_ts": started,
        "ended_ts": None,
        "history": [],
        "final_state": None,
        "error": None,
    }
    if entry is not None:
        entry.subruns[sub_id] = record
        entry.last_access = time.monotonic()

    try:
        last_step = (
            halt_after if halt_after is not None else [sub_application.graph.actions[-1].name]
        )
        _, _, final_state = await sub_application.arun(
            halt_after=last_step,
            inputs=inputs or {},
        )
        final = _serializable_state(_public_state(final_state.get_all()))[0]
        record["final_state"] = final
        record["ended_ts"] = datetime.now(UTC).isoformat()
        # If the sub-Application has its own LocalTrackingClient, surface
        # its per-step trace on the record so burr://subruns/{id} returns
        # a populated ``history`` rather than an empty list. Best-effort:
        # a missing tracker, missing file, or read error all just leave
        # ``history`` empty.
        try:
            trace_path = _tracker_log_path(sub_application)
            if trace_path is not None and trace_path.exists():
                record["history"] = _read_trace(trace_path)
        except Exception:
            pass
        return {"subrun_id": sub_id, "label": label, "final_state": final}
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        record["ended_ts"] = datetime.now(UTC).isoformat()
        raise


class ValidationFailed(Exception):
    """Raised by an input validator to refuse a call before execution.

    Validators run between MCP wire arrival and action execution. They
    receive the current public state and the inputs the client sent;
    they may raise this to refuse, return a dict to substitute
    normalised inputs, or return None to accept the originals.

    The handler catches ``ValidationFailed``, returns a structured
    ``{"error": "validation_failed", "reason": ..., "details": ...}``
    to the client, and records a refusal in ``burr://history`` with
    ``refusal_reason: "validation_failed"``. The FSM does not advance.

    Use ``details`` to attach structured per-field information (e.g.
    Pydantic validation errors) without baking it into the reason
    string.
    """

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


# Function attribute that lets hand-written Burr actions declare a
# validator without going through the importer's ``ToolSpec``. ``mount``
# reads ``_burrmcp_validator`` off each action's ``fn`` like the
# timeout attribute, so the same escape hatch works for both.
_PER_ACTION_VALIDATOR_ATTR = "_burrmcp_validator"


def _action_validator(
    action: Action,
    mount_overrides: dict[str, Callable] | None,
) -> Callable | None:
    """Return the input validator for ``action``, or None.

    A ``mount(input_validators={...})`` mapping wins over a
    function-attribute. Either source produces the same effect.
    """
    if mount_overrides is not None:
        v = mount_overrides.get(action.name)
        if v is not None:
            return v
    fn = getattr(action, "fn", None)
    if fn is None:
        return None
    return getattr(fn, _PER_ACTION_VALIDATOR_ATTR, None)


async def _run_validator(
    validator: Callable,
    state_dict: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a validator (sync or async); return normalised inputs.

    The validator may raise ``ValidationFailed`` to refuse, return a
    dict to substitute, or return None to accept the originals. Other
    exceptions propagate to the caller which wraps them as
    ``ActionExecutionError``.
    """
    if asyncio.iscoroutinefunction(validator):
        result = await validator(state_dict, inputs)
    else:
        result = validator(state_dict, inputs)
    if result is None:
        return inputs
    if not isinstance(result, dict):
        raise ValidationFailed(
            "validator returned a non-dict result",
            details={"returned_type": type(result).__name__},
        )
    return result


def _resolve(application: ApplicationOrFactory) -> tuple[Application, ApplicationFactory | None]:
    """Split ``application`` into a template instance + optional factory.

    The template is what mount-time introspection reads to register tools
    and resources. The factory, if present, is what each session calls to
    get its own isolated Application.

    Passing an ``Application`` instance returns ``(instance, None)``.
    Passing a callable returns ``(factory(), factory)``.
    """
    if isinstance(application, Application):
        return application, None
    if callable(application):
        instance = application()
        if not isinstance(instance, Application):
            raise TypeError(
                f"factory {application!r} returned {type(instance).__name__}, "
                f"expected a burr.core.Application"
            )
        return instance, application
    raise TypeError(
        f"mount() expects a burr.core.Application or a callable returning one, "
        f"got {type(application).__name__}"
    )


@dataclass
class _SessionEntry:
    """One session's slot in ``_SessionStore``.

    ``application`` is None in shared-app mode (the server has one
    Application that all sessions mutate; per-session apps aren't
    created). ``history`` is always per-session: each session sees
    only the timeline of its own calls.

    ``lock`` serializes ``app.astep`` calls within one session. Burr
    Applications are not thread-safe, and frontier clients can fire
    parallel tool calls within one MCP session (the protocol permits
    it). The lock means concurrent step calls from the same session
    queue rather than racing on the Application's state pointer.
    Different sessions still proceed in parallel.

    ``subruns`` holds the timelines of any sub-Applications spawned
    from inside this session's actions via ``burrmcp.spawn_subapp``.
    Each entry has its own id, label, started/ended timestamps,
    history list, and optional final state. Subrun ids are surfaced
    on the parent action's history entry via the ``subruns`` key so
    a client can correlate "the analyse action spawned subrun X" with
    "subrun X had the following timeline."
    """

    application: Application | None
    history: list[dict[str, Any]] = field(default_factory=list)
    subruns: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_access: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _SessionStore:
    """Lazy TTL + max-size session store.

    Eviction is lazy: stale entries are dropped on the next access
    (``get_or_create`` or any of the helpers). No background thread,
    no asyncio task, no timer surprises.

    Defaults are chosen so a small interactive server doesn't notice
    eviction at all. Long-running multi-tenant servers should tune
    ``ttl_seconds`` and ``max_sessions`` based on real session
    durations and memory budgets.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._entries: dict[str, _SessionEntry] = {}
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    def _evict_stale(self) -> None:
        if self.ttl_seconds is None:
            return
        now = time.monotonic()
        stale = [sid for sid, e in self._entries.items() if now - e.last_access > self.ttl_seconds]
        for sid in stale:
            del self._entries[sid]

    def _evict_if_full(self) -> None:
        if self.max_sessions is None:
            return
        while len(self._entries) >= self.max_sessions:
            # Evict the least-recently-accessed entry.
            oldest = min(self._entries, key=lambda s: self._entries[s].last_access)
            del self._entries[oldest]

    def get_or_create(
        self,
        sid: str,
        factory: ApplicationFactory | None,
    ) -> _SessionEntry:
        self._evict_stale()
        entry = self._entries.get(sid)
        if entry is None:
            self._evict_if_full()
            app = factory() if factory is not None else None
            entry = _SessionEntry(application=app)
            self._entries[sid] = entry
        entry.last_access = time.monotonic()
        return entry

    def history(self, sid: str) -> list[dict[str, Any]]:
        entry = self._entries.get(sid)
        return list(entry.history) if entry is not None else []

    def __len__(self) -> int:
        return len(self._entries)


def _record_history(
    store: _SessionStore,
    ctx: Context | None,
    factory: ApplicationFactory | None,
    *,
    action: str,
    inputs: dict[str, Any],
    state_after: dict[str, Any] | None,
    valid_next_actions: list[str],
    refused: bool = False,
    refusal_reason: str | None = None,
    error_message: str | None = None,
    error_type: str | None = None,
    subruns: list[str] | None = None,
) -> None:
    """Append one timeline entry to this session's history.

    Records successes and refusals alike. When ``refusal_reason`` is
    ``"action_error"``, the entry also carries ``error_message`` and
    ``error_type`` so a client can distinguish "the FSM said no" from
    "the action's code raised." When the action spawned sub-runs via
    ``spawn_subapp``, their ids are listed under ``subruns`` so the
    client can correlate the parent entry with the child timelines at
    ``burr://subruns/{id}``. No-op when ``ctx`` is None.
    """
    if ctx is None:
        return
    entry = store.get_or_create(ctx.session_id, factory)
    record: dict[str, Any] = {
        "seq": len(entry.history),
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "inputs": inputs,
        "state_after": state_after,
        "valid_next_actions": valid_next_actions,
        "refused": refused,
        "refusal_reason": refusal_reason,
    }
    if error_message is not None:
        record["error_message"] = error_message
    if error_type is not None:
        record["error_type"] = error_type
    if subruns:
        record["subruns"] = subruns
        record["subrun_uris"] = [f"burr://subruns/{sid}" for sid in subruns]
    entry.history.append(record)
    entry.last_access = time.monotonic()


def _refusal_payload(
    *,
    exc: Exception,
    action_name: str,
    app: Application,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a step-side exception into an MCP refusal payload + history kwargs.

    Single source of truth for the four-way error-to-refusal translation.
    Returns ``(response_dict, history_kwargs)``: the caller hands the
    first back to the MCP client and splats the second into
    ``_record_history(... state_after=None, **history_kwargs)``.

    ``valid_next_action_names(app)`` is computed once and shared between
    the response and the history entry.
    """
    if isinstance(exc, InvalidTransitionError):
        return (
            {
                "error": "invalid_transition",
                "requested": exc.requested,
                "valid_next_actions": exc.valid,
                "message": str(exc),
            },
            {
                "refused": True,
                "refusal_reason": "invalid_transition",
                "valid_next_actions": exc.valid,
            },
        )
    valid = valid_next_action_names(app)
    if isinstance(exc, ValidationFailed):
        return (
            {
                "error": "validation_failed",
                "requested": action_name,
                "reason": exc.reason,
                "details": exc.details,
                "valid_next_actions": valid,
            },
            {
                "refused": True,
                "refusal_reason": "validation_failed",
                "valid_next_actions": valid,
                "error_message": exc.reason,
                "error_type": "ValidationFailed",
            },
        )
    if isinstance(exc, ActionTimeoutError):
        return (
            {
                "error": "action_timeout",
                "requested": action_name,
                "timeout_seconds": exc.timeout_seconds,
                "message": str(exc),
                "valid_next_actions": valid,
            },
            {
                "refused": True,
                "refusal_reason": "action_timeout",
                "valid_next_actions": valid,
                "error_message": str(exc),
                "error_type": "TimeoutError",
            },
        )
    if isinstance(exc, ActionExecutionError):
        return (
            {
                "error": "action_error",
                "requested": action_name,
                "error_type": type(exc.original).__name__,
                "error_message": str(exc.original),
                "valid_next_actions": valid,
            },
            {
                "refused": True,
                "refusal_reason": "action_error",
                "valid_next_actions": valid,
                "error_message": str(exc.original),
                "error_type": type(exc.original).__name__,
            },
        )
    raise exc  # not one of ours; let it propagate


def _expects_object_or_array(prop_schema: dict[str, Any]) -> bool:
    """Return True if the given JSON Schema fragment allows object/array.

    Handles three shapes: direct ``"type": "object"``, list-of-types
    ``"type": ["object", "null"]``, and union forms ``anyOf`` / ``oneOf``.
    """
    if not isinstance(prop_schema, dict):
        return False
    schema_type = prop_schema.get("type")
    if isinstance(schema_type, str) and schema_type in {"object", "array"}:
        return True
    if isinstance(schema_type, list) and any(t in {"object", "array"} for t in schema_type):
        return True
    for variant in (*prop_schema.get("anyOf", ()), *prop_schema.get("oneOf", ())):
        if isinstance(variant, dict) and variant.get("type") in {"object", "array"}:
            return True
    return False


def _build_coercion_middleware():
    """Build the JSON-string-to-object coercion middleware.

    Lazily imports the FastMCP Middleware base so this file doesn't pay
    the import cost when nobody calls ``mount()``.
    """
    from fastmcp.server.middleware import Middleware as _Mw

    class _CoerceJsonStringInputs(_Mw):
        """Coerce JSON-string values to objects/arrays when the schema asks.

        Some MCP clients serialize nested object arguments as JSON strings
        (e.g. sending ``"inputs": "{\\"item\\": \\"mocha\\"}"`` instead of
        ``"inputs": {"item": "mocha"}``). FastMCP's input-schema validator
        then rejects with ``params/X must be object``. This middleware
        intercepts ``tools/call`` and re-parses any string value whose
        declared schema is object- or array-typed.

        Schema lookup is built lazily on first call by reading the live
        tool list off the server, then cached for the lifetime of the
        middleware instance.
        """

        def __init__(self) -> None:
            self._schemas: dict[str, dict[str, Any]] | None = None

        async def _build_lookup(self, server: FastMCP) -> dict[str, dict[str, Any]]:
            tools = await server.list_tools(run_middleware=False)
            out: dict[str, dict[str, Any]] = {}
            for tool in tools:
                # FastMCP's internal Tool exposes the schema as ``parameters``;
                # the MCP wire-level Tool exposes it as ``inputSchema``.
                schema = (
                    getattr(tool, "parameters", None) or getattr(tool, "inputSchema", None) or {}
                )
                out[tool.name] = schema.get("properties", {}) or {}
            return out

        async def on_call_tool(self, context, call_next):
            msg = context.message
            args = getattr(msg, "arguments", None)
            if not args:
                return await call_next(context)

            if self._schemas is None:
                fctx = getattr(context, "fastmcp_context", None)
                if fctx is None or getattr(fctx, "fastmcp", None) is None:
                    return await call_next(context)
                self._schemas = await self._build_lookup(fctx.fastmcp)

            param_schemas = self._schemas.get(msg.name)
            if not param_schemas:
                return await call_next(context)

            new_args: dict[str, Any] | None = None
            for key, value in args.items():
                if not isinstance(value, str):
                    continue
                if not _expects_object_or_array(param_schemas.get(key, {})):
                    continue
                try:
                    parsed = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(parsed, (dict, list)):
                    continue
                if new_args is None:
                    new_args = dict(args)
                new_args[key] = parsed

            if new_args is not None:
                new_msg = msg.model_copy(update={"arguments": new_args})
                context = context.copy(message=new_msg)
            return await call_next(context)

    return _CoerceJsonStringInputs()


def _session_app_and_lock(
    ctx: Context | None,
    shared_app: Application,
    shared_lock: asyncio.Lock,
    factory: ApplicationFactory | None,
    store: _SessionStore,
) -> tuple[Application, asyncio.Lock, _SessionEntry | None]:
    """Resolve the ``(Application, lock, session_entry)`` for this request.

    Shared-app mode (factory is None): returns ``shared_app`` plus the
    server-wide ``shared_lock``. All sessions serialise their step
    calls on this lock, because they're all mutating one Application.
    The session entry is the per-session bookkeeping slot (history,
    sub-runs); it still exists in shared-app mode for history's sake.

    Factory mode: returns the session's own Application and the
    session entry's own lock. Different sessions' steps run in
    parallel; calls within one session queue on its lock.

    ``ctx`` may be None when invoked outside an MCP request; in that
    case ``entry`` is None and the app/lock come from the server-wide
    defaults.
    """
    if ctx is None:
        return shared_app, shared_lock, None
    entry = store.get_or_create(ctx.session_id, factory)
    if factory is None:
        return shared_app, shared_lock, entry
    assert entry.application is not None  # factory mode guarantees this
    return entry.application, entry.lock, entry


async def _step_application(
    app: Application,
    action_name: str,
    inputs: dict[str, Any],
    timeout_seconds: float | None = None,
    validator: Callable | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run one step of the Application.

    Refuses to step unless ``action_name`` is in the current valid-next
    set; otherwise raises ``InvalidTransitionError``. ``timeout_seconds``
    wraps the call in ``asyncio.wait_for``. ``validator`` runs after the
    transition check and may raise ``ValidationFailed``, return None to
    accept the originals, or return a dict to substitute normalised
    inputs. Action-body exceptions are wrapped as ``ActionExecutionError``
    so callers can record them structurally.
    """
    valid = valid_next_action_names(app)
    if action_name not in valid:
        raise InvalidTransitionError(action_name, valid)

    if validator is not None:
        inputs = await _run_validator(validator, _public_state(app.state.get_all()), inputs)

    # Force Burr to run the specifically-requested action. ``astep``
    # picks via ``self.get_next_action()``, which returns the first
    # transition whose condition is true. In a branching graph that
    # isn't necessarily the action the client named, so we override
    # ``get_next_action`` for one call. Tracker hooks, state updates,
    # and ``__PRIOR_STEP`` housekeeping all flow through Burr's normal
    # ``_astep`` machinery; only the action selection is forced.
    target_action = app.graph.get_action(action_name)
    if target_action is None:
        raise InvalidTransitionError(action_name, valid)
    is_streaming = bool(getattr(target_action, "streaming", False))
    original_get_next_action = app.get_next_action
    app.get_next_action = lambda: target_action  # type: ignore[method-assign]
    try:
        if is_streaming:
            return await _step_streaming_action(
                app=app,
                action_name=action_name,
                inputs=inputs,
                ctx=ctx,
                timeout_seconds=timeout_seconds,
            )
        if timeout_seconds is not None:
            a, result, new_state = await asyncio.wait_for(
                app.astep(inputs=inputs), timeout=timeout_seconds
            )
        else:
            a, result, new_state = await app.astep(inputs=inputs)
    except (InvalidTransitionError, ActionExecutionError, ActionTimeoutError):
        raise
    except TimeoutError as exc:
        raise ActionTimeoutError(action_name, timeout_seconds or 0.0) from exc
    except Exception as exc:
        # Anything raised by the wrapped action's fn comes out here.
        # Wrap so the handler can record a structured refusal entry.
        raise ActionExecutionError(action_name, exc) from exc
    finally:
        app.get_next_action = original_get_next_action  # type: ignore[method-assign]
    state, coerced = _serializable_state(_public_state(new_state.get_all()))
    if coerced:
        state["_burrmcp"] = {"coerced_keys": coerced}
    return {
        "action": a.name,
        "result": result,
        "state": state,
        "valid_next_actions": valid_next_action_names(app),
        "app_id": app.uid,
        "tracker_project": _tracker_project(app),
    }


async def _step_streaming_action(
    *,
    app: Application,
    action_name: str,
    inputs: dict[str, Any],
    ctx: Context | None,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """Run a streaming Burr action and surface chunks as MCP progress
    notifications.

    Burr streaming actions yield intermediate chunks plus a final
    state. We forward each chunk to the client via
    ``ctx.report_progress`` (the MCP-spec mechanism for partial
    results during a long-running tool call), then return the final
    state in the same shape as a regular step response. When ``ctx``
    is None the chunks are still iterated but not surfaced; the final
    state is returned regardless.

    Cancellation on timeout is best-effort. The streaming container's
    iterator is wrapped in ``asyncio.wait_for`` per-chunk so individual
    chunks can time out, with the total budget shared across them.
    """
    try:
        a, container = await app.astream_result(halt_after=[action_name], inputs=inputs)
    except Exception as exc:
        raise ActionExecutionError(action_name, exc) from exc

    chunk_count = 0
    try:
        async for chunk in container:
            chunk_count += 1
            if ctx is not None:
                # ``report_progress`` accepts a string message; chunks
                # may be arbitrary JSON-serialisable values, so we
                # stringify them deterministically for the wire.
                try:
                    msg = json.dumps(chunk, default=str)
                except (TypeError, ValueError):
                    msg = str(chunk)
                # Progress notifications are best-effort: clients that
                # didn't supply a progress token cause this to drop on
                # the floor. Suppressing here keeps the action running
                # in the face of a noisy/disconnected notification
                # channel.
                with contextlib.suppress(Exception):
                    await ctx.report_progress(progress=chunk_count, message=msg)
        final_chunk, final_state_dict = await container.get()
    except TimeoutError as exc:
        raise ActionTimeoutError(action_name, timeout_seconds or 0.0) from exc
    except Exception as exc:
        raise ActionExecutionError(action_name, exc) from exc

    state, coerced = _serializable_state(_public_state(final_state_dict))
    if coerced:
        state["_burrmcp"] = {"coerced_keys": coerced}
    return {
        "action": a.name,
        "result": final_chunk,
        "state": state,
        "valid_next_actions": valid_next_action_names(app),
        "app_id": app.uid,
        "tracker_project": _tracker_project(app),
        "streamed": True,
        "chunks": chunk_count,
    }


async def _emit_log(ctx: Context | None, msg: str) -> None:
    if ctx is None:
        return
    with contextlib.suppress(Exception):
        await ctx.info(msg)


def _step_tool_result(body: dict[str, Any], headline: str) -> ToolResult:
    return ToolResult(
        content=[
            TextContent(type="text", text=headline),
            TextContent(type="text", text=json.dumps(body, default=str)),
        ],
        structured_content=body,
    )


def _success_headline(seq: int, action: str, valid_next: list[str]) -> str:
    if valid_next:
        peek = ",".join(valid_next[:3])
        more = "" if len(valid_next) <= 3 else f"+{len(valid_next) - 3}"
        return f"Step {seq}: {action} ✓ → {peek}{more}"
    return f"Step {seq}: {action} ✓ (terminal)"


def _refusal_headline(seq: int, action: str, refusal_reason: str, detail: str = "") -> str:
    tail = f" ({detail})" if detail else ""
    return f"Step {seq}: {action} ✗ {refusal_reason}{tail}"


def _has_local_tracker(app: Application) -> bool:
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return False
    return isinstance(getattr(app, "_tracker", None), LocalTrackingClient)


def mount(
    application: ApplicationOrFactory,
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
    session_ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
    max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    action_timeout_seconds: float | None = None,
    input_validators: dict[str, Callable] | None = None,
    state_loader: Any | None = None,
) -> FastMCP:
    """Return a FastMCP server that exposes ``application`` per ``mode``.

    Args:
        application: Either a built ``burr.core.Application`` (shared
            across all sessions) or a callable ``() -> Application``
            (called once per session for state isolation). The graph
            shape is read once at mount time, so factories should
            return Applications with the same graph each call.
        mode: ``ServingMode.STEP`` (the only supported value).
        name: MCP server name; defaults to ``"burrmcp"``.
        instructions: Server-level instructions surfaced via the MCP
            spec's server-info ``instructions`` field.
        session_ttl_seconds: Idle TTL for the per-session store. After
            this many seconds without a tool call or resource read, a
            session's Application and history are evicted on the next
            access. Set to ``None`` to disable TTL eviction. Default
            3600 (1 hour).
        max_sessions: Hard cap on simultaneous live sessions in the
            store. When exceeded, the least-recently-accessed entry
            is evicted on insert. Set to ``None`` to disable size
            eviction. Default 100.
        action_timeout_seconds: Server-wide timeout applied to every
            action invocation. Wraps ``app.astep`` in
            ``asyncio.wait_for``; on expiry the action's coroutine is
            cancelled and the call returns an ``action_timeout`` error
            without advancing state. ``None`` (default) means no
            timeout. Cancellation is prompt for async actions doing
            I/O; for sync or CPU-bound work it's best-effort.
        input_validators: Optional mapping of action name to a
            validator callable ``(state_dict, inputs) -> dict | None``.
            Runs before action dispatch. May raise ``ValidationFailed``
            to refuse the call, return a dict to substitute normalised
            inputs, or return None to accept the originals. Async
            validators are supported. ToolSpec-declared validators
            from the importer also work; per-action attribute
            ``fn._burrmcp_validator`` is the hand-tagged escape hatch.
        state_loader: Optional Burr ``BaseStateLoader`` (SQLite, S3,
            Postgres, etc.) used by ``fork_from_past``. Resolution order:
            this loader wins; else the current Application's
            ``LocalTrackingClient``; else refuse.
    """
    shared_app, factory = _resolve(application)
    # Per-session store keyed by ctx.session_id; populated lazily on
    # the first tool call. Lives in closure scope so it's tied to this
    # server instance, not module-global. Holds both the session's
    # Application (factory mode only) and its history (always).
    store = _SessionStore(
        ttl_seconds=session_ttl_seconds,
        max_sessions=max_sessions,
    )
    # Lock used in shared-app mode (one Application, many sessions).
    # In factory mode each session uses its own ``entry.lock`` instead.
    shared_lock = asyncio.Lock()

    server_name = name or "burrmcp"
    # Static graph summary, computed once. Sub-runs may have their own
    # graphs but this resource describes the top-level one.
    graph_summary = _compute_graph_summary(shared_app, server_name)
    graph_summary_json = json.dumps(graph_summary, indent=2)
    # Augment user-supplied instructions with a one-line hint pointing
    # at burr://graph. Cold-start discoverability without forcing users
    # to write the hint themselves.
    action_surface = _render_action_surface(shared_app)
    discovery_hint = (
        "Read burr://graph once at start for full per-action metadata "
        "(reads, writes, required/optional inputs); the listing above is "
        "the minimum surface. You don't need to keep polling burr://next "
        "or burr://state, each step response already includes the new "
        "state and valid_next_actions inline. To restart the FSM after "
        "reaching a terminal node or a dead-end branch, call the "
        "reset_session tool. To rewind to a specific earlier point and "
        "explore an alternate path from there, call fork_at(sequence_id) "
        "with a seq from burr://history. Both are always available."
    )
    parts = [p for p in (instructions, action_surface, discovery_hint) if p]
    effective_instructions = "\n\n".join(parts)
    # ``strict_input_validation=False`` lets the coercion middleware below
    # run before FastMCP rejects out-of-shape arguments. With strict
    # validation on, the MCP SDK validates JSON-RPC params against the
    # tool's input schema before middleware ever sees them, so a client
    # that sends ``inputs: "{\"item\":\"mocha\"}"`` (string) instead of
    # ``inputs: {"item":"mocha"}`` (object) gets rejected at the SDK
    # layer. Off plus the coercion middleware means the middleware
    # parses the string back to a dict, and Burr's own action-input
    # checking then validates at the action layer.
    mcp = FastMCP(
        server_name,
        instructions=effective_instructions,
        strict_input_validation=False,
    )
    # Tolerate clients that JSON-encode object-typed arguments as strings
    # (e.g. IBM Bob as of mid-2026). The middleware re-parses such strings
    # to objects/arrays when the tool's declared schema says the param
    # should be one. Tools whose schemas say "string" are not touched.
    mcp.add_middleware(_build_coercion_middleware())

    # ── resources ────────────────────────────────────────────────────

    @mcp.resource("burr://graph")
    async def _graph_resource() -> str:
        """Static description of the Application's FSM topology.

        Read once per session. The graph doesn't change after mount;
        a model that has this resource doesn't need to keep polling
        ``burr://next`` to plan ahead. Each tool response already
        carries the current state and the valid next actions, so
        runtime polling is only useful for forensic inspection of an
        already-running session.

        Shape:

            {
              "name": "<server name>",
              "entrypoint": "<starting action>",
              "actions": [
                {"name", "description", "reads", "writes",
                 "required_inputs", "optional_inputs"}, ...
              ],
              "transitions": [
                {"from", "to", "condition": "<expr or null>"}, ...
              ]
            }
        """
        return graph_summary_json

    @mcp.resource("burr://state")
    async def _state_resource(ctx: Context) -> str:
        """Current Application state as JSON.

        Internal Burr keys (``__PRIOR_STEP``, ``__SEQUENCE_ID``) are
        filtered. Non-JSON-representable values are coerced to strings,
        with the affected keys listed under ``_burrmcp.coerced_keys``
        so the client knows the round-trip is lossy.
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        state, coerced = _serializable_state(_public_state(app.state.get_all()))
        if coerced:
            state["_burrmcp"] = {"coerced_keys": coerced}
        return json.dumps(state, indent=2)

    @mcp.resource("burr://next")
    async def _next_resource(ctx: Context) -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        return json.dumps(valid_next_action_names(app))

    @mcp.resource("burr://history")
    async def _history_resource(ctx: Context) -> str:
        """Timeline of every action attempted in this session.

        Each entry has ``seq``, ``ts``, ``action``, ``inputs``,
        ``state_after``, ``valid_next_actions``, ``refused``, and
        ``refusal_reason``. Both successful steps and refused attempts
        (invalid transitions, unknown actions) appear. In factory-mode
        deployments each session sees only its own history; in
        shared-app deployments each session sees the timeline of its
        own calls against the shared FSM.
        """
        history = store.history(ctx.session_id) if ctx is not None else []
        return json.dumps(history, default=str, indent=2)

    @mcp.resource("burr://subruns")
    async def _subruns_resource(ctx: Context) -> str:
        """Index of sub-Application runs spawned in this session.

        Each entry has ``id``, ``uri``, ``label``, ``started_ts``,
        ``ended_ts``, and the ``parent_action`` that spawned it. The
        ``uri`` field is the fully-rendered ``burr://subruns/{id}``
        address, ready to read without constructing it from a template.
        Empty list if no actions in this session called
        ``spawn_subapp``.
        """
        if ctx is None:
            return json.dumps([])
        entry = store.get_or_create(ctx.session_id, factory)
        index = []
        # Reverse-map subrun_id -> the parent history entry that spawned it.
        parent_action_for: dict[str, str] = {}
        for h in entry.history:
            for sid in h.get("subruns", []) or []:
                parent_action_for[sid] = h["action"]
        for sid, record in entry.subruns.items():
            index.append(
                {
                    "id": sid,
                    "uri": f"burr://subruns/{sid}",
                    "label": record.get("label"),
                    "started_ts": record.get("started_ts"),
                    "ended_ts": record.get("ended_ts"),
                    "parent_action": parent_action_for.get(sid),
                    "error": record.get("error"),
                }
            )
        return json.dumps(index, default=str, indent=2)

    @mcp.resource("burr://subruns/{subrun_id}")
    async def _subrun_detail_resource(subrun_id: str, ctx: Context) -> str:
        """Full record for one sub-Application run.

        Includes the sub-run's id, label, parent-spawning timestamps,
        in-memory history of the sub-graph's steps, final public
        state, and any error that aborted the run. Returns
        ``{"error": "unknown_subrun"}`` if the id isn't known to this
        session.
        """
        if ctx is None:
            return json.dumps({"error": "no_session"})
        entry = store.get_or_create(ctx.session_id, factory)
        record = entry.subruns.get(subrun_id)
        if record is None:
            return json.dumps(
                {
                    "error": "unknown_subrun",
                    "subrun_id": subrun_id,
                    "known_subruns": list(entry.subruns),
                },
                indent=2,
            )
        return json.dumps(record, default=str, indent=2)

    @mcp.resource("burr://trace")
    async def _trace_resource(ctx: Context) -> str:
        """Burr's on-disk LocalTrackingClient log for this session's Application.

        Returns the JSONL records Burr writes for every action step
        (action enter/exit, state diff, timing). The Application must
        have been built with ``.with_tracker(LocalTrackingClient(...))``
        for this resource to return data; otherwise the response is
        ``{"error": "no_tracker", "message": "..."}``.

        Responses are capped at the most recent 1000 records to keep
        the wire payload bounded. For full traces, read the log file
        directly off disk at the path Burr's tracker writes to.

        This is the cross-reference between burrmcp's in-memory
        ``burr://history`` (one entry per attempted action, including
        refusals) and Burr's own structured trace format (one entry
        per state transition, full Burr replay shape).
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        path = _tracker_log_path(app)
        if path is None:
            return json.dumps(
                {
                    "error": "no_tracker",
                    "message": (
                        "This Application has no LocalTrackingClient attached. "
                        "Pass tracker=LocalTrackingClient(project='...') to "
                        "ApplicationBuilder.with_tracker(...) when building "
                        "the Application to enable burr://trace."
                    ),
                },
                indent=2,
            )
        if not path.exists():
            return json.dumps([])
        return json.dumps(_read_trace(path), default=str, indent=2)

    @mcp.resource("burr://session")
    async def _session_resource(ctx: Context) -> str:
        """Tracker coordinates for the current MCP session's Application.

        Returns ``{project, app_id, app_dir, partition_key}`` so a client
        (or the agent itself) can locate this session's tracker data on
        disk without guessing. Useful for terminal tooling like
        ``burrmcp watch <project>`` that tails the LocalTrackingClient
        JSONL, and for any out-of-band inspection of
        ``~/.burr/<project>/<app-id>/log.jsonl``.

        ``project`` and ``app_dir`` are null when no
        ``LocalTrackingClient`` is attached; ``app_id`` and
        ``partition_key`` are always populated because they live on the
        Application directly.
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        project: str | None = None
        app_dir: str | None = None
        try:
            from burr.tracking.client import LocalTrackingClient
        except ImportError:
            LocalTrackingClient = None  # type: ignore[assignment]
        tracker = getattr(app, "_tracker", None)
        if LocalTrackingClient is not None and isinstance(tracker, LocalTrackingClient):
            project = tracker.project_id
            try:
                storage_dir = Path(tracker.storage_dir).expanduser().resolve()
                app_dir = str((storage_dir / app.uid).resolve())
            except (OSError, AttributeError):
                app_dir = None
        return json.dumps(
            {
                "project": project,
                "app_id": app.uid,
                "app_dir": app_dir,
                "partition_key": getattr(app, "_partition_key", None),
            },
            indent=2,
        )

    # ── tools, per mode ──────────────────────────────────────────────

    if mode is ServingMode.STEP:
        action_names = [a.name for a in shared_app.graph.actions]
        action_map = {a.name: a for a in shared_app.graph.actions}

        async def step(
            action: str,
            inputs: dict[str, Any] | str | None = None,
            ctx: Context | None = None,
        ) -> ToolResult:
            """Advance the FSM by one transition.

            Args:
                action: Name of the action to run. Must be in the
                    current valid-next set; otherwise the call returns
                    an ``invalid_transition`` error with the list of
                    actions actually allowed right now.
                inputs: Keyword inputs to the action. Each action
                    declares its own required + optional inputs;
                    consult ``burr://next`` and the action's docstring
                    to see what's expected. Object is the canonical
                    form. A JSON-encoded string is also accepted (some
                    clients serialize nested object arguments that way)
                    and is parsed into an object before dispatch.
            """
            # Coerce a JSON-string inputs into a dict so the body always
            # sees the canonical shape. The schema advertises both forms
            # so clients that validate against the schema don't reject
            # the string-encoded path before sending the request.
            if isinstance(inputs, str):
                try:
                    parsed = json.loads(inputs)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                inputs = parsed if isinstance(parsed, dict) else None
            # Peek the seq that this call's history entry will get, so
            # the ctx.info headline matches the recorded seq.
            seq = len(store.history(ctx.session_id)) if ctx is not None else 0
            if action not in action_map:
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    valid_next_actions=action_names,
                    refused=True,
                    refusal_reason="unknown_action",
                )
                body = {
                    "error": "unknown_action",
                    "requested": action,
                    "known_actions": action_names,
                }
                headline = f"Step {seq}: {action} ✗ unknown_action"
                await _emit_log(ctx, headline)
                return _step_tool_result(body, headline)
            app, lock, entry = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
            effective_timeout = _action_timeout(action_map[action], action_timeout_seconds)
            effective_validator = _action_validator(action_map[action], input_validators)
            token = _current_session_entry.set(entry)
            ctx_token = _current_fastmcp_context.set(ctx)
            subruns_before = set(entry.subruns) if entry is not None else set()
            try:
                async with lock:
                    out = await _step_application(
                        app,
                        action_name=action,
                        inputs=inputs or {},
                        timeout_seconds=effective_timeout,
                        validator=effective_validator,
                        ctx=ctx,
                    )
            except (
                ValidationFailed,
                InvalidTransitionError,
                ActionTimeoutError,
                ActionExecutionError,
            ) as exc:
                response, hist_kwargs = _refusal_payload(exc=exc, action_name=action, app=app)
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    **hist_kwargs,
                )
                headline = _refusal_headline(
                    seq,
                    action,
                    response["error"],
                    detail=response.get("error_type", "") or "",
                )
                await _emit_log(ctx, headline)
                return _step_tool_result(response, headline)
            finally:
                _current_session_entry.reset(token)
                _current_fastmcp_context.reset(ctx_token)
            new_subruns: list[str] = []
            if entry is not None:
                new_subruns = [s for s in entry.subruns if s not in subruns_before]
            _record_history(
                store,
                ctx,
                factory,
                action=action,
                inputs=inputs or {},
                state_after=out["state"],
                valid_next_actions=out["valid_next_actions"],
                subruns=new_subruns or None,
            )
            headline = _success_headline(seq, action, out["valid_next_actions"])
            await _emit_log(ctx, headline)
            return _step_tool_result(out, headline)

        step_description = f"{step.__doc__}\n\n{action_surface}"
        mcp.tool(
            name="step",
            description=step_description,
            output_schema=_step_response_schema(),
        )(step)

    else:
        raise ValueError(f"unknown serving mode: {mode!r}")

    # ── meta tool: reset_session ────────────────────────────────────
    # Always callable regardless of FSM state. Only meaningful in
    # factory mode; refuses in shared-app mode. The discovery hint in
    # ``instructions`` advertises it so the agent doesn't have to ask
    # the human to restart the server when it reaches a terminal node
    # and wants to try another path.

    async def reset_session(ctx: Context | None = None) -> ToolResult | dict[str, Any]:
        """Reset this session's FSM to its entrypoint.

        Rebuilds the session's Application via the factory, clears any
        sub-runs the session spawned, and appends a ``reset_session``
        marker entry to ``burr://history``. Prior history entries are
        preserved, so the audit trail records the reset rather than
        wiping it: ``ran A -> ran B -> reset -> ran A again``.

        Refuses in shared-app mode (servers mounted with an
        ``Application`` instance rather than a factory) because
        resetting would affect every connected client at once. Use
        per-session isolation (factory mode) for servers where reset
        matters.
        """
        if factory is None:
            return _step_tool_result(
                {
                    "error": "reset_not_supported",
                    "reason": (
                        "this server runs in shared-app mode (no factory was passed "
                        "to mount); resetting would affect every connected client. "
                        "Disconnect and reconnect for a fresh session, or remount "
                        "the server with a factory: mount(() -> Application, ...)"
                    ),
                },
                "reset_session ✗ shared-app mode",
            )
        if ctx is None:
            return _step_tool_result({"error": "no_session"}, "reset_session ✗ no_session")

        entry = store.get_or_create(ctx.session_id, factory)
        async with entry.lock:
            previous_state, _ = _serializable_state(
                _public_state(entry.application.state.get_all())
                if entry.application is not None
                else {}
            )
            entry.application = factory()
            entry.subruns.clear()
            new_app = entry.application
            assert new_app is not None
            new_state, coerced = _serializable_state(_public_state(new_app.state.get_all()))
            if coerced:
                new_state["_burrmcp"] = {"coerced_keys": coerced}
            valid_next = valid_next_action_names(new_app)
            entry.last_access = time.monotonic()

        _record_history(
            store,
            ctx,
            factory,
            action="reset_session",
            inputs={},
            state_after=new_state,
            valid_next_actions=valid_next,
        )

        headline = f"Session reset → {new_app.entrypoint}"
        await _emit_log(ctx, headline)
        return _step_tool_result(
            {
                "action": "reset_session",
                "result": {"previous_state": previous_state},
                "state": new_state,
                "valid_next_actions": valid_next,
                "app_id": new_app.uid,
                "tracker_project": _tracker_project(new_app),
            },
            headline,
        )

    mcp.tool(name="reset_session", description=reset_session.__doc__)(reset_session)

    # ── meta tool: fork_at ──────────────────────────────────────────
    # Rewind the session's Application to the state captured after a
    # specific history entry. Lets an agent explore "what if" branches
    # without disconnecting and losing context. Implemented via our
    # in-memory history rather than Burr's tracker-based replay so it
    # works without requiring users to wire up a LocalTrackingClient.

    async def fork_at(sequence_id: int, ctx: Context | None = None) -> ToolResult | dict[str, Any]:
        """Rewind the session to the state captured after history[seq=N].

        ``sequence_id`` is the ``seq`` field on a ``burr://history`` entry.
        The session's Application is rebuilt via the factory, then its
        state is overwritten with the snapshot captured at that point,
        and its ``__PRIOR_STEP`` is set to the action name from that
        entry so ``valid_next_actions`` computes correctly. Sub-runs
        recorded after that point are cleared. A ``fork_at`` marker is
        appended to history with the target sequence_id under
        ``inputs``.

        Refuses when:
          - shared-app mode (would affect every connected client);
          - sequence_id is out of range;
          - the target entry was a refusal (state_after is None);
          - the target entry is itself a fork or reset marker (avoid
            walking a hall of mirrors).
        """
        if factory is None:
            return _step_tool_result(
                {
                    "error": "fork_not_supported",
                    "reason": (
                        "this server runs in shared-app mode (no factory was passed "
                        "to mount); forking would affect every connected client. "
                        "Remount with a factory to enable fork_at."
                    ),
                },
                "fork_at ✗ shared-app mode",
            )
        if ctx is None:
            return _step_tool_result({"error": "no_session"}, "fork_at ✗ no_session")

        entry = store.get_or_create(ctx.session_id, factory)
        async with entry.lock:
            if sequence_id < 0 or sequence_id >= len(entry.history):
                return _step_tool_result(
                    {
                        "error": "unknown_sequence_id",
                        "requested": sequence_id,
                        "history_length": len(entry.history),
                    },
                    f"fork_at ✗ unknown_sequence_id ({sequence_id})",
                )
            target = entry.history[sequence_id]
            if target.get("refused"):
                return _step_tool_result(
                    {
                        "error": "cannot_fork_to_refusal",
                        "sequence_id": sequence_id,
                        "refusal_reason": target.get("refusal_reason"),
                    },
                    f"fork_at ✗ cannot_fork_to_refusal (seq={sequence_id})",
                )
            if target.get("action") in {"fork_at", "reset_session"}:
                return _step_tool_result(
                    {
                        "error": "cannot_fork_to_meta_entry",
                        "sequence_id": sequence_id,
                        "action": target.get("action"),
                    },
                    f"fork_at ✗ cannot_fork_to_meta_entry (seq={sequence_id})",
                )
            saved_state = target.get("state_after")
            if saved_state is None:
                return _step_tool_result(
                    {"error": "no_state_snapshot", "sequence_id": sequence_id},
                    f"fork_at ✗ no_state_snapshot (seq={sequence_id})",
                )
            target_action = target.get("action")

            # Keep sub-runs spawned at or before the fork point; the
            # parent history entries that reference them are still
            # visible, so dropping them would leave dangling links.
            kept_subrun_ids: set[str] = {
                sid for h in entry.history[: sequence_id + 1] for sid in (h.get("subruns") or [])
            }
            kept_subruns = {
                sid: rec for sid, rec in entry.subruns.items() if sid in kept_subrun_ids
            }

            new_app, new_state, valid_next = _restore_snapshot(
                entry=entry,
                factory=factory,
                state_dict=saved_state,
                last_action=target_action,
                sequence_id_override=sequence_id,
                kept_subruns=kept_subruns,
            )

        _record_history(
            store,
            ctx,
            factory,
            action="fork_at",
            inputs={"sequence_id": sequence_id},
            state_after=new_state,
            valid_next_actions=valid_next,
        )

        headline = f"Forked to seq={sequence_id} ({target_action})"
        await _emit_log(ctx, headline)
        return _step_tool_result(
            {
                "action": "fork_at",
                "result": {
                    "sequence_id": sequence_id,
                    "from_action": target_action,
                },
                "state": new_state,
                "valid_next_actions": valid_next,
                "app_id": new_app.uid,
                "tracker_project": _tracker_project(new_app),
            },
            headline,
        )

    mcp.tool(name="fork_at", description=fork_at.__doc__)(fork_at)

    # ── meta tool: fork_from_past ───────────────────────────────────
    # Resume a past Burr run from disk. Lets an agent recover state
    # after a server restart, or fork from any persisted past app_id
    # the client has tracked. Requires:
    #   - factory mode (need to rebuild the Application)
    #   - the session's current Application to have a
    #     LocalTrackingClient attached (so we know which storage_dir
    #     and project to read from)

    async def fork_from_past(
        app_id: str,
        sequence_id: int = -1,
        partition_key: str = "",
        ctx: Context | None = None,
    ) -> ToolResult | dict[str, Any]:
        """Resume a past Burr run by loading persisted state.

        Three-tier source resolution:

        1. If ``mount(state_loader=...)`` was passed an explicit Burr
           ``BaseStateLoader``, use it. Any persister works:
           ``SQLitePersister``, custom S3/postgres loaders, etc.
        2. Else if the session's current Application has a
           ``LocalTrackingClient`` attached, read its on-disk log.
        3. Else refuse.

        ``partition_key`` defaults to empty string, matching Burr's
        default; pass it explicitly when your persister uses
        partitioned storage.

        Use this for:
          - resuming a session across server restarts (track
            ``app_id`` on the client, restore here after reconnect);
          - forking from any persisted past run, not just the current
            session's in-memory history.

        Refuses when:
          - shared-app mode (no factory to rebuild from);
          - no state_loader configured and no LocalTrackingClient on
            the Application;
          - the requested app_id/sequence_id doesn't exist.
        """
        if factory is None:
            return _step_tool_result(
                {
                    "error": "fork_not_supported",
                    "reason": (
                        "this server runs in shared-app mode (no factory was passed "
                        "to mount); cross-session resume requires per-session "
                        "isolation. Remount with a factory."
                    ),
                },
                "fork_from_past ✗ shared-app mode",
            )
        if ctx is None:
            return _step_tool_result({"error": "no_session"}, "fork_from_past ✗ no_session")

        entry = store.get_or_create(ctx.session_id, factory)
        async with entry.lock:
            loaded_state_dict: dict[str, Any] | None = None
            last_action: str | None = None

            if state_loader is not None:
                # Tier 1: explicit BaseStateLoader passed to mount().
                # Works with any persister (SQLite, S3, postgres, etc.).
                try:
                    raw = state_loader.load(
                        partition_key=partition_key,
                        app_id=app_id,
                        sequence_id=sequence_id if sequence_id != -1 else None,
                    )
                    if asyncio.iscoroutine(raw):
                        loaded = await raw
                    else:
                        loaded = raw
                except Exception as exc:
                    return _step_tool_result(
                        {
                            "error": "unknown_past_run",
                            "reason": str(exc),
                            "app_id": app_id,
                            "sequence_id": sequence_id,
                        },
                        f"fork_from_past ✗ unknown_past_run ({app_id})",
                    )
                if loaded is None:
                    return _step_tool_result(
                        {
                            "error": "unknown_past_run",
                            "reason": "loader returned None",
                            "app_id": app_id,
                            "sequence_id": sequence_id,
                        },
                        f"fork_from_past ✗ unknown_past_run ({app_id})",
                    )
                # PersistedStateData has state as a burr State; pull
                # the dict out and let the rebuild path normalise.
                loaded_state_obj = loaded["state"]
                loaded_state_dict = loaded_state_obj.get_all()
                last_action = loaded.get("position")
            else:
                # Tier 2: fall back to LocalTrackingClient on the app.
                try:
                    from burr.tracking.client import LocalTrackingClient
                except ImportError:
                    return _step_tool_result({"error": "no_tracker"}, "fork_from_past ✗ no_tracker")
                tracker = getattr(entry.application, "_tracker", None)
                if not isinstance(tracker, LocalTrackingClient):
                    return _step_tool_result(
                        {
                            "error": "no_tracker",
                            "reason": (
                                "no state_loader passed to mount() and the current "
                                "Application has no LocalTrackingClient. Either "
                                "pass `state_loader=<BaseStateLoader>` to mount() "
                                "or add `.with_tracker(LocalTrackingClient(...))` "
                                "to the factory."
                            ),
                        },
                        "fork_from_past ✗ no_tracker",
                    )
                try:
                    import warnings

                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        loaded_state_dict, last_action = LocalTrackingClient.load_state(
                            project=tracker.project_id,
                            app_id=app_id,
                            sequence_id=sequence_id,
                            storage_dir=tracker.raw_storage_dir,
                        )
                except (ValueError, FileNotFoundError, OSError) as exc:
                    return _step_tool_result(
                        {
                            "error": "unknown_past_run",
                            "reason": str(exc),
                            "app_id": app_id,
                            "sequence_id": sequence_id,
                        },
                        f"fork_from_past ✗ unknown_past_run ({app_id})",
                    )

            # In-memory subruns belonged to the previous session state,
            # which has been replaced; clear all of them.
            new_app, new_state, valid_next = _restore_snapshot(
                entry=entry,
                factory=factory,
                state_dict=loaded_state_dict,
                last_action=last_action,
            )

        _record_history(
            store,
            ctx,
            factory,
            action="fork_from_past",
            inputs={"app_id": app_id, "sequence_id": sequence_id},
            state_after=new_state,
            valid_next_actions=valid_next,
        )

        headline = f"Resumed app_id={app_id} seq={sequence_id}"
        await _emit_log(ctx, headline)
        return _step_tool_result(
            {
                "action": "fork_from_past",
                "result": {
                    "loaded_app_id": app_id,
                    "loaded_sequence_id": sequence_id,
                    "from_action": last_action,
                },
                "state": new_state,
                "valid_next_actions": valid_next,
                "app_id": new_app.uid,
                "tracker_project": _tracker_project(new_app),
            },
            headline,
        )

    mcp.tool(name="fork_from_past", description=fork_from_past.__doc__)(fork_from_past)

    # Surfaces resources as tools for clients without resources/read
    # (IBM Bob Shell, as of mid-2026).
    from fastmcp.server.transforms import ResourcesAsTools, Visibility

    mcp.add_transform(ResourcesAsTools(mcp))

    # Without a tracker or loader, fork_from_past can only refuse.
    if state_loader is None and not _has_local_tracker(shared_app):
        mcp.add_transform(Visibility(False, names={"fork_from_past"}))

    return mcp


_NAMESPACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def mount_multi(
    applications: dict[str, ApplicationOrFactory],
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
    session_ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
    max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    action_timeout_seconds: float | None = None,
) -> FastMCP:
    """Mount multiple Burr Applications as one MCP server.

    Each application is wrapped by ``mount()`` independently, then
    composed into a parent FastMCP via FastMCP's native server-
    composition (``parent.mount(sub, namespace=...)``). Namespacing
    rules follow FastMCP:

    * Tools are renamed ``<app>_<tool>``. In STEP mode this means each
      app exposes ``<app>_step``, ``<app>_reset_session``,
      ``<app>_fork_at``, ``<app>_fork_from_past``.
    * Resources keep their scheme but get the namespace inserted:
      ``burr://graph`` from app ``order`` becomes ``burr://order/graph``.

    Args:
        applications: Mapping of namespace name to Application or
            factory. Names must be valid Python identifiers
            (alphanumeric + underscore, starting with a letter) so the
            FastMCP namespacing rule produces clean tool / resource
            names.
        mode: Serving mode applied to every sub-application.
        name: Parent server name surfaced to MCP clients.
        instructions: Server-level instructions for the parent. The
            per-app instructions (with the auto-described action
            surface) remain on each sub-server.
        session_ttl_seconds, max_sessions, action_timeout_seconds:
            Forwarded to each sub-application's ``mount()`` call.

    A ``burr://apps`` resource on the parent lists the mounted app
    names so a connecting agent can discover the namespace surface in
    one read.
    """
    if not applications:
        raise ValueError("mount_multi requires at least one application")
    invalid = [n for n in applications if not _NAMESPACE_RE.match(n)]
    if invalid:
        raise ValueError(
            f"namespace names must match {_NAMESPACE_RE.pattern!r}; got invalid: {invalid}"
        )

    parent_name = name or "burrmcp-multi"
    parent_lines: list[str] = []
    if instructions:
        parent_lines.append(instructions)
    parent_lines.append(
        "Multi-Application server. The following Burr Applications are "
        "mounted side by side, namespaced by app name:"
    )
    parent_lines.extend(f"  - {app_name}" for app_name in sorted(applications))
    parent_lines.append(
        "Tools are renamed <app>_<tool>; resources are accessible as "
        "burr://<app>/<path>. Read `burr://apps` for the live list."
    )
    parent_instructions = "\n\n".join(parent_lines)

    parent = FastMCP(parent_name, instructions=parent_instructions)

    for app_name, app_or_factory in applications.items():
        sub = mount(
            app_or_factory,
            mode=mode,
            name=app_name,
            session_ttl_seconds=session_ttl_seconds,
            max_sessions=max_sessions,
            action_timeout_seconds=action_timeout_seconds,
        )
        parent.mount(sub, namespace=app_name)

    namespace_list = sorted(applications)

    @parent.resource("burr://apps")
    async def _apps_resource() -> str:
        """List the apps mounted on this multi-Application server."""
        return json.dumps({"apps": namespace_list}, indent=2)

    return parent
