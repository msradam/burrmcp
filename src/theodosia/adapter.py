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
                             schema and ``theodosia://graph``.

The mount registers eight resources:

  • ``theodosia://graph``:           static description of the FSM topology
                                (actions, reads/writes/inputs, edges
                                with conditions). Read once per session.
  • ``theodosia://state``:           current Application state as JSON.
  • ``theodosia://next``:            actions reachable from current state.
  • ``theodosia://history``:         per-session timeline of every action
                                attempt (successes + refusals).
  • ``theodosia://trace``:           Burr's on-disk LocalTrackingClient log.
  • ``theodosia://session``:         tracker coordinates (project, app_id,
                                app_dir, partition_key) for locating
                                this session's data on disk.
  • ``theodosia://subruns``:         index of sub-Application runs spawned
                                in this session via ``spawn_subapp``.
  • ``theodosia://subruns/{id}``:    full record for one sub-run.

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
import io
import json
import logging
import os
import re
import time
import typing
import uuid
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from theodosia.persona import PersonaSource

import pydantic
from burr.core import Application
from burr.core.action import Action
from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from theodosia.ledger import HashChainedLedger
from theodosia.upstream import UpstreamManager, bind_upstream, reset_upstream

ApplicationFactory = Callable[[], Application]
ApplicationOrFactory = Application | ApplicationFactory

# Defaults for session-store eviction.
_DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour idle
_DEFAULT_MAX_SESSIONS = 100


class ServingMode(str, Enum):  # noqa: UP042  # (str, Enum) for stable wire serialization
    STEP = "step"
    # ``TOOLS`` (one MCP tool per @action, no enforcement) and ``DYNAMIC``
    # (per-session ``tools/list_changed`` visibility) were carved out into
    # ``theodosia._experimental.modes`` after STEP became the sole product.
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
    """Refusal: requested action name is not in the FSM.

    Carries the same steering fields as ``_StepInvalidTransition``
    (``valid_next_actions`` + ``message``, plus a ``next_hint`` appended
    by the reactive-hint layer) so a model that hallucinated a name can
    recover from the response alone. ``known_actions`` is retained for
    spotting typos against the full namespace.
    """

    error: typing.Literal["unknown_action"]
    requested: str
    known_actions: list[str]
    valid_next_actions: list[str]
    message: str


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
            "  - unknown_action: requested + known_actions + valid_next_actions + message\n"
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
                "description": (
                    "Human-readable message (unknown_action, invalid_transition, action_timeout)."
                ),
            },
            "next_hint": {
                "type": "string",
                "description": (
                    "Directional steering string appended after every step and "
                    "refusal: cites what just happened and the reachable actions "
                    "now. Present on success and on every refusal."
                ),
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
            "next_external_tools": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
                "description": (
                    "Present only when the server was mounted with external_tools. "
                    "Maps each currently-reachable action to the tools (on other "
                    "connected MCP servers) relevant before taking it. Call those "
                    "tools, then step() to record findings and advance."
                ),
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
_TRACE_MAX_ENTRIES = 1000  # cap theodosia://trace response to the last N records

from theodosia._introspect import (  # noqa: E402,F401 (re-export)
    _INTERNAL_STATE_KEYS,
    _PER_ACTION_TIMEOUT_ATTR,
    _action_timeout,
    _public_state,
    _serializable_state,
)


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

    forked_state_dict: dict[str, Any] = state_dict | {"__PRIOR_STEP": last_action}
    if sequence_id_override is not None:
        forked_state_dict["__SEQUENCE_ID"] = sequence_id_override
    new_app.update_state(_BurrState(forked_state_dict))

    entry.subruns = kept_subruns if kept_subruns is not None else {}

    new_state, coerced = _serializable_state(_public_state(new_app.state.get_all()))
    if coerced:
        new_state["_theodosia"] = {"coerced_keys": coerced}
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


# The machinery preamble shipped to every Theodosia-served MCP server by
# default. It teaches the agent how to drive the workflow: one step tool,
# pick from the listed actions, recover from refusals by reading
# valid_next_actions, stop at terminal. Validated in the floor test (qwen3:0.6b
# went from 0/10 to 10/10 on a trivial 3-action FSM once this was present).
# Mount() prepends this to any developer-supplied ``instructions`` unless
# ``include_default_instructions=False`` is passed.
DEFAULT_INSTRUCTIONS = """\
This server exposes several tools. To drive the workflow, use only the `step` \
tool. Follow this loop:

1. Call `step` with {"action": "<NAME>", "inputs": {}} where <NAME> is one of \
the action names listed in the `step` tool's schema.

2. If the response contains "valid_next_actions": [...], your call was \
refused. You MUST call `step` again with one of the names in that list. Do \
not write a text reply.

3. If the response contains "known_actions" but the action you used is not in \
the list, you used a name that does not exist. Pick a name from \
"known_actions" or "valid_next_actions" and call `step` again.

4. Stop when the response shows a terminal state (no "valid_next_actions" \
listed)."""


def _render_action_surface(app: Application) -> str:
    """Render a compact text summary of the FSM's action + transition surface.

    Appended to the server's `instructions` so an MCP client sees the
    action namespace at connect time, before reading any resources. The
    first line of each action's docstring (if any) becomes its summary;
    transitions show source and target, plus a `(when: expr)` clause for
    conditional edges. Inputs are deliberately omitted; they live on the
    `step` tool's argument schema (or `theodosia://graph` for full detail).
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

    lines.extend(("", "Transitions:"))
    for t in app.graph.transitions:
        cond = getattr(t.condition, "_name", None) or getattr(t.condition, "name", None)
        if cond and cond != "default":
            lines.append(f"  - {t.from_.name} -> {t.to.name}  (when: {cond})")
        else:
            lines.append(f"  - {t.from_.name} -> {t.to.name}")
    return "\n".join(lines)


def _normalize_external_tools(
    external_tools: dict[str, list[str]] | None, app: Application
) -> dict[str, list[str]]:
    """Keep only entries whose action name exists in the graph. Warn on
    unknowns rather than failing, so a typo is recoverable."""
    if not external_tools:
        return {}
    known = {a.name for a in app.graph.actions}
    out: dict[str, list[str]] = {}
    for action_name, tools in external_tools.items():
        if action_name not in known:
            warnings.warn(
                f"external_tools names unknown action {action_name!r}; "
                f"known actions: {sorted(known)}. Ignoring this entry.",
                stacklevel=3,
            )
            continue
        out[action_name] = [t for t in (tools or ()) if isinstance(t, str) and t.strip()]
    return out


def _next_external_tools(
    external_tools_map: dict[str, list[str]], valid_next_actions: list[str]
) -> dict[str, list[str]]:
    """Per-reachable-action external tools, for surfacing in step responses.

    Only includes reachable actions that actually declare external tools,
    so the response stays empty (omitted by the caller) for FSMs that
    don't use the feature.
    """
    return {a: external_tools_map[a] for a in valid_next_actions if external_tools_map.get(a)}


def _compute_graph_summary(
    app: Application,
    server_name: str,
    external_tools_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build a static description of an Application's graph.

    Computed once at mount time and returned as-is by the
    ``theodosia://graph`` resource. Includes per-action metadata
    (description, reads, writes, required/optional inputs) and the
    full transition table including conditions as printed expressions.

    The point of this surface is cold-start discovery: a model
    connecting to the server can read one resource and have the full
    topology without trial-and-error or repeated state probes.
    """
    ext_map = external_tools_map or {}
    actions_meta: list[dict[str, Any]] = []
    for a in app.graph.actions:
        required, optional = _action_inputs(a)
        fn = getattr(a, "fn", None)
        doc = (fn.__doc__ or "").strip() if fn is not None and fn.__doc__ else ""
        input_schemas = _input_schemas(a)
        meta: dict[str, Any] = {
            "name": a.name,
            "description": doc,
            "reads": list(a.reads or []),
            "writes": list(a.writes or []),
            "required_inputs": required,
            "optional_inputs": optional,
            "input_schemas": input_schemas,
        }
        if ext_map.get(a.name):
            meta["external_tools"] = ext_map[a.name]
        actions_meta.append(meta)

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
                    "``theodosia://history``). Lets an agent explore alternate "
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


def _build_persona_frame(
    ctx: Context | None,
    store: _SessionStore,
    factory: ApplicationFactory | None,
) -> dict[str, Any] | None:
    """Build the frame dict for persona interpolation, or ``None`` if no session.

    Top-level keys: ``state.<field>``, ``action.name``, ``action.reachable``,
    ``graph.total_actions``, ``graph.all_actions``, ``session.session_id``.
    """
    if ctx is None or factory is None:
        return None
    try:
        session_id = ctx.session_id
    except Exception:
        return None
    entry = store.get_or_create(session_id, factory)
    app = entry.application
    if app is None:
        return None
    state_fields = dict(app.state.get_all())
    state_fields.pop("__PRIOR_STEP", None)
    last_action = app.state.get("__PRIOR_STEP") or app.entrypoint
    reachable = valid_next_action_names(app)
    return {
        "state": state_fields,
        "action": {
            "name": last_action,
            "reachable": ", ".join(reachable),
        },
        "graph": {
            "total_actions": len(app.graph.actions),
            "all_actions": ", ".join(a.name for a in app.graph.actions),
        },
        "session": {"session_id": session_id},
    }


from theodosia._exceptions import (  # noqa: E402 (re-export)
    ActionExecutionError,
    ActionTimeoutError,
    InvalidTransitionError,
    ValidationFailed,
)
from theodosia._introspect import (  # noqa: E402,F401 (re-export)
    _action_inputs,
    _action_signature_params,
    _annotation_to_schema,
    _coerce_pydantic_inputs,
    _input_schemas,
    _pydantic_model_in_annotation,
    valid_next_action_names,
)

#: ContextVar set by the step handler around each ``_step_application``
#: call. Reads inside an action body see their session's entry; used by
#: ``spawn_subapp`` to record sub-run timelines on the parent session.
_current_subrun_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_theodosia_current_subrun_id", default=None
)
_current_session_entry: contextvars.ContextVar[_SessionEntry | None] = contextvars.ContextVar(
    "_theodosia_current_session", default=None
)

#: ContextVar set by the step handler around each ``_step_application``
#: call. Holds the FastMCP ``Context`` injected by the MCP transport so
#: action bodies can call ``ctx.sample(...)``, ``ctx.elicit(...)``,
#: ``ctx.report_progress(...)``, ``ctx.read_resource(...)``, etc.
#: Reads via the public ``current_mcp_context()`` helper return the
#: current value (or ``None`` outside an action body).
_current_fastmcp_context: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "_theodosia_current_fastmcp_context", default=None
)


def current_mcp_context() -> Context | None:
    """Return the FastMCP ``Context`` for the currently-dispatching tool call.

    Returns ``None`` outside an action body. Inside an action driven via
    theodosia's adapter, returns the Context FastMCP injected into the
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
    surfaced via ``theodosia://subruns`` and ``theodosia://subruns/{subrun_id}``.

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
        "parent_subrun_id": _current_subrun_id.get(),
        "history": [],
        "final_state": None,
        "error": None,
    }
    if entry is not None:
        entry.subruns[sub_id] = record
        entry.last_access = time.monotonic()

    halt_after_names = set(
        halt_after if halt_after is not None else [sub_application.graph.actions[-1].name]
    )
    subrun_token = _current_subrun_id.set(sub_id)
    try:
        # Drive the sub-app step-by-step so ``record["history"]`` populates
        # without requiring the sub-Application to wire its own tracker.
        # Each step lands in the record as it completes; halt_after is
        # checked after the action runs (Burr's arun semantics).
        step_inputs: dict[str, Any] = dict(inputs or {})
        final_state = sub_application.state
        seq = 0
        while True:
            stepped = await sub_application.astep(inputs=step_inputs)
            if stepped is None:
                break
            action_obj, _result, new_state = stepped
            step_state = _serializable_state(_public_state(new_state.get_all()))[0]
            record["history"].append({"seq": seq, "action": action_obj.name, "state": step_state})
            seq += 1
            final_state = new_state
            step_inputs = {}  # only the first astep carries client inputs
            if action_obj.name in halt_after_names:
                break
        final = _serializable_state(_public_state(final_state.get_all()))[0]
        record["final_state"] = final
        record["ended_ts"] = datetime.now(UTC).isoformat()
        return {"subrun_id": sub_id, "label": label, "final_state": final}
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        record["ended_ts"] = datetime.now(UTC).isoformat()
        raise
    finally:
        _current_subrun_id.reset(subrun_token)


# Function attribute that lets hand-written Burr actions declare a
# validator without going through the importer's ``ToolSpec``. ``mount``
# reads ``_theodosia_validator`` off each action's ``fn`` like the
# timeout attribute, so the same escape hatch works for both.
_PER_ACTION_VALIDATOR_ATTR = "_theodosia_validator"


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


from theodosia._session import (  # noqa: E402 (re-export for adapter backcompat)
    _DEFAULT_MAX_SESSIONS,
    _DEFAULT_SESSION_TTL_SECONDS,
    _SessionEntry,
    _SessionStore,
)


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
    app: Application | None = None,
) -> None:
    """Append one timeline entry to this session's history.

    Records successes and refusals alike. When ``refusal_reason`` is
    ``"action_error"``, the entry also carries ``error_message`` and
    ``error_type`` so a client can distinguish "the FSM said no" from
    "the action's code raised." When the action spawned sub-runs via
    ``spawn_subapp``, their ids are listed under ``subruns`` so the
    client can correlate the parent entry with the child timelines at
    ``theodosia://subruns/{id}``. No-op when ``ctx`` is None.
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
        record["subrun_uris"] = [f"theodosia://subruns/{sid}" for sid in subruns]
    entry.history.append(record)
    entry.last_access = time.monotonic()
    # In factory mode the session app is entry.application; in shared-app mode it
    # lives outside the entry, so the caller passes it. Either way the durable
    # artifacts need the app that actually owns the tracker.
    durable_app = app if app is not None else entry.application
    if durable_app is not None:
        if refused:
            _append_refusal_sidecar(durable_app, record)
        _append_ledger(durable_app, record)


def _ledger_binding(app: Application, log_path: Path) -> dict[str, Any]:
    """Identity fields hashed into every ledger entry.

    Embedding these in the chain means copying ``ledger.jsonl`` between
    session directories breaks verification: ``verify`` is called with the
    on-disk ``app_id`` / ``project`` and refuses entries whose binding does
    not match.
    """
    return {
        "app_id": log_path.parent.name,
        "project": log_path.parent.parent.name,
        "partition_key": getattr(app, "partition_key", None),
    }


def _append_ledger(app: Application, record: dict[str, Any]) -> None:
    """Chain one attempt (step or refusal) onto the session's tamper-evident
    ledger, next to the tracker log.

    Unlike ``refusals.jsonl`` (refusals only, for ``theodosia logs --refusals``),
    the ledger covers every attempt and is hash-chained, so ``theodosia verify``
    can detect any after-the-fact edit. No-op when the Application has no local
    tracker.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    with contextlib.suppress(OSError):
        ledger = HashChainedLedger(
            log_path.parent / "ledger.jsonl",
            binding=_ledger_binding(app, log_path),
        )
        ledger.append(record)


def _append_refusal_sidecar(app: Application, record: dict[str, Any]) -> None:
    """Persist a refusal next to the Burr tracker log, so the durable audit
    trail includes blocked transitions, not just executed steps.

    Burr's ``LocalTrackingClient`` only logs actions that ran, so an
    ``invalid_transition`` (the graph blocking an out-of-order call) never
    reaches the on-disk log. We append it to a ``refusals.jsonl`` sidecar in the
    same app directory; ``theodosia logs --refusals`` reads both. No-op when the
    Application has no local tracker.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    sidecar = log_path.parent / "refusals.jsonl"
    with contextlib.suppress(OSError), sidecar.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


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


# ── Reactive hinting (auto-hint layer) ─────────────────────────────────────
#
# After every step (success or refusal), Theodosia appends a single ``next_hint``
# string to the response. The hint is generated in two layers:
#
#   1. Auto-hint (this module): derived from graph introspection alone -- what
#      transitions are reachable, what kind of refusal just happened, whether
#      the session is terminal. No domain knowledge required; works for any
#      Burr graph mounted via mount().
#
#   2. Domain hint (caller-supplied next_hint callback): receives the same
#      structural signals plus the refusal payload, and can return a
#      domain-specific override. When provided and non-None, it replaces the
#      auto-hint; otherwise the auto-hint is used.
#
# The split mirrors POSIX: errno (structural taxonomy) lives at the kernel
# layer; strerror (semantic translation) lives at the libc layer.


def _auto_hint_success(action: str, valid_next: list[str]) -> str | None:
    """Structural hint after a successful step.

    Terminal nodes get a "session terminal" signal. Otherwise an enumeration
    of reachable actions. Both are derivable from the Burr graph alone -- no
    domain knowledge is consumed.
    """
    if not valid_next:
        return "Session is at a terminal state. No further actions are reachable."
    head = ", ".join(valid_next[:6])
    more = f" (+{len(valid_next) - 6} more)" if len(valid_next) > 6 else ""
    return f"Reachable actions from current state: {head}{more}."


def _auto_hint_refusal(refusal: dict[str, Any]) -> str | None:
    """Structural hint after a refusal.

    Maps the Theodosia refusal taxonomy (unknown_action / invalid_transition /
    validation_failed / action_timeout / action_error) to short, model-readable
    strings that cite the structural reason without claiming to know the domain.
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
    for backwards compatibility with three-arg callbacks, the refusal arg
    is omitted when the inspected signature can't accept it. Domain hint
    wins iff it returns a non-empty string; otherwise the structural
    auto-hint is used.
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


async def _race_with_timeout(coro: typing.Awaitable[Any], timeout_seconds: float) -> Any:
    """Run ``coro`` with a hard wall-clock budget.

    Unlike :func:`asyncio.wait_for`, which waits for the cancelled task to
    propagate ``CancelledError`` before returning, this returns at the
    budget boundary regardless. The orphaned task continues in the
    background until its own internals unwind (FastMCP's ``ctx.sample`` /
    ``ctx.elicit`` notably do not honor cancellation cleanly because they
    are waiting on a server-to-client request; ``wait_for`` would sit until
    the FastMCP request timeout, defeating the action-level budget).

    Raises :class:`TimeoutError` at the budget; the caller translates it
    into an :class:`ActionTimeoutError` refusal.
    """
    task = asyncio.ensure_future(coro)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if task in done:
            return task.result()
        task.cancel()
        raise TimeoutError(f"action did not complete within {timeout_seconds}s")
    except BaseException:
        if not task.done():
            task.cancel()
        raise


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

    # Coerce dict-valued inputs into their declared Pydantic model types so
    # the action body receives the typed object it annotated. Without this
    # the action signature says ``order: OrderInput`` but the body sees a
    # plain dict.
    inputs = _coerce_pydantic_inputs(target_action, inputs)
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
        # astep is typed Optional, but the monkey-patched get_next_action
        # guarantees a non-None return for the client-named action.
        # Suppress Burr's own stderr traceback display. The action error
        # surfaces as a structured ``action_error`` refusal on the wire; the
        # traceback would print before that wire response and add noise to
        # the developer's terminal. Set THEODOSIA_VERBOSE=1 to keep it.
        if os.environ.get("THEODOSIA_VERBOSE"):
            stderr_ctx: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
        else:
            stderr_ctx = contextlib.redirect_stderr(io.StringIO())
        # A sync action body would block the event loop, which defeats
        # ``asyncio.wait_for``: the cancellation timer cannot tick while the
        # body is running. Detect sync bodies and run the step in a thread
        # so blocking happens off the main loop and the timeout can fire.
        # The orphaned thread finishes in the background (Python cannot
        # safely kill threads), but the client gets a clean
        # ``ActionTimeoutError`` refusal. Async bodies stay on the main
        # loop where ctx-injection works.
        fn = getattr(target_action, "fn", None)
        is_sync_body = fn is not None and not inspect.iscoroutinefunction(fn)
        with stderr_ctx:
            if timeout_seconds is not None and is_sync_body:

                def _thread_runner() -> tuple[Any, Any, Any]:
                    return asyncio.run(app.astep(inputs=inputs))  # type: ignore[return-value,misc]

                a, result, new_state = await _race_with_timeout(
                    asyncio.to_thread(_thread_runner), timeout_seconds
                )
            elif timeout_seconds is not None:
                a, result, new_state = await _race_with_timeout(  # type: ignore[misc]
                    app.astep(inputs=inputs), timeout_seconds
                )
            else:
                a, result, new_state = await app.astep(inputs=inputs)  # type: ignore[misc]
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
        state["_theodosia"] = {"coerced_keys": coerced}
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

    state, coerced = _serializable_state(_public_state(final_state_dict))  # type: ignore[arg-type]
    if coerced:
        state["_theodosia"] = {"coerced_keys": coerced}
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


def _attach_hooks(app: Application, hooks: list[Any]) -> None:
    """Append Burr lifecycle adapters to an already-built ``Application``.

    Burr's ``ApplicationBuilder.with_hooks(*)`` is the normal path; this
    helper is the post-build version for callers that only see the built
    Application (factories, deserialized apps). Uses Burr's public
    ``LifecycleAdapterSet.with_new_adapters`` so we don't reach into
    ``_adapters`` or ``_get_lifecycle_hooks`` ourselves; Burr does the
    same in ``Application.__init__`` to wire its ``TracerFactoryContextHook``.
    """
    adapter_set = getattr(app, "_adapter_set", None)
    if adapter_set is None:
        return
    app._adapter_set = adapter_set.with_new_adapters(*hooks)


def _resolve_assembly_workflow(workflow: Any) -> Any:
    """Turn an Assembly's ``workflow`` field into a built Application or factory."""
    if not isinstance(workflow, str):
        return workflow
    import importlib

    module_name, _, attr = workflow.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _mount_from_assembly(asm: Any, **kw: Any) -> FastMCP:
    """Recurse into ``mount`` with the Assembly's fields, letting explicit kwargs win."""
    workflow = _resolve_assembly_workflow(asm.workflow)
    return mount(
        workflow,
        mode=kw["mode"],
        name=kw["name"] if kw["name"] is not None else asm.name,
        instructions=kw["instructions"] if kw["instructions"] is not None else asm.instructions,
        include_default_instructions=kw["include_default_instructions"]
        if not kw["include_default_instructions"]
        else asm.include_default_instructions,
        personas=kw["personas"] if kw["personas"] is not None else asm.personas,
        default_persona=kw["default_persona"]
        if kw["default_persona"] is not None
        else asm.default_persona,
        session_ttl_seconds=kw["session_ttl_seconds"],
        max_sessions=kw["max_sessions"],
        action_timeout_seconds=kw["action_timeout_seconds"],
        input_validators=kw["input_validators"],
        state_loader=kw["state_loader"],
        next_hint=kw["next_hint"],
        external_tools=kw["external_tools"],
        upstream=kw["upstream"] if kw["upstream"] is not None else asm.upstream,
        hooks=kw.get("hooks"),
        middleware=kw.get("middleware"),
    )


def _silence_fastmcp_loggers() -> None:
    """Quiet FastMCP's per-call DEBUG output unless THEODOSIA_VERBOSE is set.

    FastMCP's notification log is useful when wiring a new client but noisy
    in normal use. ``THEODOSIA_VERBOSE=1`` restores it.
    """
    if os.environ.get("THEODOSIA_VERBOSE"):
        return
    for _noisy in ("fastmcp", "mcp", "FastMCP"):
        _lg = logging.getLogger(_noisy)
        if _lg.level < logging.WARNING:
            _lg.setLevel(logging.WARNING)


def mount(
    application: ApplicationOrFactory | Any,
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
    include_default_instructions: bool = True,
    personas: PersonaSource | None = None,
    default_persona: str | None = None,
    session_ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
    max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    action_timeout_seconds: float | None = None,
    input_validators: dict[str, Callable] | None = None,
    state_loader: Any | None = None,
    next_hint: Callable[..., str | None] | None = None,
    external_tools: dict[str, list[str]] | None = None,
    upstream: dict[str, Any] | None = None,
    hooks: list[Any] | None = None,
    middleware: list[Any] | None = None,
) -> FastMCP:
    """Return a FastMCP server that exposes ``application`` per ``mode``.

    Args:
        application: Either a built ``burr.core.Application`` (shared
            across all sessions) or a callable ``() -> Application``
            (called once per session for state isolation). The graph
            shape is read once at mount time, so factories should
            return Applications with the same graph each call.
        mode: ``ServingMode.STEP`` (the only supported value).
        name: MCP server name; defaults to ``"theodosia"``.
        instructions: Server-level instructions surfaced via the MCP
            spec's server-info ``instructions`` field. When
            ``include_default_instructions`` is True (the default), the
            machinery preamble (``DEFAULT_INSTRUCTIONS``) is prepended to
            this string so the agent gets both how-to-drive-an-FSM guidance
            and your FSM-specific guidance. Pass an empty string to suppress
            the developer portion while still keeping the default preamble.
        include_default_instructions: Whether to prepend
            ``DEFAULT_INSTRUCTIONS`` (the validated 5-rule machinery preamble)
            to the server's instructions. Default True. Floor-tested to lift
            qwen3:0.6b from 0/10 to 10/10 on a trivial FSM, with no regression
            on stronger models. Pass False if you want full control over what
            the agent sees.
        personas: An optional identity layer. Accepts a directory path (parses
            every ``*.md`` file as a PERSONA.md), a single file path, a dict
            ``{name: text}``, or a list of ``Persona`` objects. Each persona is
            registered as an MCP prompt named ``theodosia/persona/<name>`` so
            clients can pick one at session-start, and the full set is
            exposed at ``theodosia://personas``. If unset, the server has no
            identity layer (the historical Theodosia behavior).
        default_persona: The persona whose body is prepended to the server's
            ``instructions`` for clients that don't pick one explicitly. Must
            be one of the loaded persona names, or ``None`` (default) to use
            the lexically first persona. Ignored if no personas are loaded.
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
            ``fn._theodosia_validator`` is the hand-tagged escape hatch.
        state_loader: Optional Burr ``BaseStateLoader`` (SQLite, S3,
            Postgres, etc.) used by ``fork_from_past``. Resolution order:
            this loader wins; else the current Application's
            ``LocalTrackingClient``; else refuse.
        next_hint: Optional callback for domain-specific reactive
            guidance. Receives ``(state, valid_next_actions, last_action,
            refusal=None)`` and returns a single-line natural-language
            hint, or ``None`` to defer to the structural auto-hint.
            Called after every step success and every refusal. The
            returned string (or the auto-hint when this returns None)
            is appended to the step response as ``next_hint``. Closes
            the gap weaker agent models hit when enumeration of valid
            actions isn't enough -- they need direction, not just a list.
            Backwards-compatible: three-arg callbacks (without the
            ``refusal`` parameter) still work.
        external_tools: Experimental, advisory. Prefer ``upstream`` for
            driving other MCP servers. This is the fallback for the case
            where Theodosia cannot be in the call path (the agent reaches a
            server Theodosia can't, e.g. a separate auth or network
            boundary). Maps an action name to tool names on OTHER MCP
            servers the agent is connected to. Surfaced per-action in
            ``theodosia://graph`` and contextually as ``next_external_tools``
            in each ``step`` response, as advisory guidance only: the
            agent calls those tools on its own connected servers, then
            ``step()``s. Theodosia neither executes nor validates them.
            Being a two-surface design, it relies on the model's
            discipline and works best with capable models. Unknown action
            names are ignored with a warning at mount time.
        upstream: Optional mapping of server name to a ``fastmcp.Client``
            transport spec (a URL string, an mcp-config dict, or a
            transport object). theodosia opens an MCP *client* session to
            each and binds them so action bodies can call their tools via
            ``theodosia.call_upstream(server, tool, args)``. Unlike
            ``external_tools`` (advisory; the agent calls tools on its own
            connected servers), ``upstream`` puts theodosia in the call path:
            the agent sees only theodosia's ``step`` tool, the upstream
            servers are not exposed to it, and every upstream call happens
            inside an action so it advances state by construction. This is
            the single-surface, ledger-honest way to drive any MCP server
            from a graph, and it works with any compliant server because
            ``fastmcp.Client`` speaks every transport. Sessions open
            lazily on first use and stay open for the server's lifetime.
        hooks: Optional list of Burr ``LifecycleAdapter`` instances
            (``PreRunStepHook``, ``PostRunStepHook``, ``PreStartStreamHook``,
            ``DoLogAttributeHook``, etc.). Attached to every session's
            Application after construction. Equivalent to calling
            ``ApplicationBuilder.with_hooks(...)`` inside the factory, but
            keeps adapter-side concerns (timing, custom telemetry sinks,
            structured logging) out of the factory.
        middleware: Optional list of FastMCP ``Middleware`` instances added
            to the mounted server after Theodosia's built-in input-coercion
            middleware. Useful for OTel spans on every MCP call, rate
            limiting, structured logging, or per-call metrics; the
            ``with_middleware`` example demo uses this pattern with
            ``TimingMiddleware`` / ``StructuredLoggingMiddleware`` /
            ``RateLimitingMiddleware``. Order matters: middleware added
            earlier wraps later ones, so these run inside the coercion
            layer (they see post-coercion args).
    """
    from theodosia.assembly import Assembly

    if isinstance(application, Assembly):
        return _mount_from_assembly(
            application,
            mode=mode,
            name=name,
            instructions=instructions,
            include_default_instructions=include_default_instructions,
            personas=personas,
            default_persona=default_persona,
            session_ttl_seconds=session_ttl_seconds,
            max_sessions=max_sessions,
            action_timeout_seconds=action_timeout_seconds,
            input_validators=input_validators,
            state_loader=state_loader,
            next_hint=next_hint,
            external_tools=external_tools,
            upstream=upstream,
            hooks=hooks,
            middleware=middleware,
        )

    _silence_fastmcp_loggers()
    shared_app, factory = _resolve(application)

    # When user-supplied hooks are provided, wrap shared_app or factory so
    # the hooks are attached after construction. Burr's adapter set is a
    # plain list of LifecycleAdapter instances; we append and re-derive the
    # sync/async hook caches. Hooks attached this way fire on the same
    # surfaces as ``ApplicationBuilder.with_hooks(...)``; they are not
    # session-scoped, so place per-session logic inside the hook body if
    # needed.
    if hooks:
        if factory is not None:
            original_factory = factory

            def factory_with_hooks() -> Application:
                app_inst = original_factory()
                _attach_hooks(app_inst, hooks)
                return app_inst

            factory = factory_with_hooks
        if shared_app is not None:
            _attach_hooks(shared_app, hooks)
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

    server_name = name or "theodosia"
    # Normalize the external-tools map: keep only entries whose action
    # name exists in the graph; warn (don't fail) on unknowns so a typo
    # doesn't take the server down.
    external_tools_map = _normalize_external_tools(external_tools, shared_app)
    # Accept either a config dict (the common case; UpstreamManager opens
    # real client sessions) or a pre-built manager (test fakes, custom
    # managers wrapping already-open sessions). Anything with an async
    # ``call`` attribute is treated as a manager; anything else is treated
    # as a config dict.
    upstream_manager: Any
    if upstream is None:
        upstream_manager = None
    elif hasattr(upstream, "call"):
        upstream_manager = upstream
    else:
        upstream_manager = UpstreamManager(upstream)
    # Static graph summary, computed once. Sub-runs may have their own
    # graphs but this resource describes the top-level one.
    graph_summary = _compute_graph_summary(shared_app, server_name, external_tools_map)
    graph_summary_json = json.dumps(graph_summary, indent=2)
    # Augment user-supplied instructions with a one-line hint pointing
    # at theodosia://graph. Cold-start discoverability without forcing users
    # to write the hint themselves.
    action_surface = _render_action_surface(shared_app)
    discovery_hint = (
        "Read theodosia://graph once at start for full per-action metadata "
        "(reads, writes, required/optional inputs); the listing above is "
        "the minimum surface. You don't need to keep polling theodosia://next "
        "or theodosia://state, each step response already includes the new "
        "state and valid_next_actions inline. To restart the FSM after "
        "reaching a terminal node or a dead-end branch, call the "
        "reset_session tool. To rewind to a specific earlier point and "
        "explore an alternate path from there, call fork_at(sequence_id) "
        "with a seq from theodosia://history. Both are always available."
    )
    # Load personas (the identity layer). One persona becomes the default for
    # this server's instructions; all personas are registered as MCP prompts
    # so clients can pick a different one at session-start.
    from theodosia.persona import load_personas, resolve_default

    personas_map = load_personas(personas)
    persona = resolve_default(personas_map, default_persona)
    persona_prompt = persona.to_prompt_text() if persona is not None else None

    # Compose the server's instructions in this order:
    #   1. DEFAULT_INSTRUCTIONS: the machinery preamble (how to drive an FSM)
    #   2. default persona prompt: who's executing the workflow (identity)
    #   3. instructions: developer-supplied (what this specific FSM is for)
    #   4. action_surface: the rendered action listing
    #   5. discovery_hint: pointer to ``theodosia://graph`` etc.
    # Order matters: machinery first (protocol), identity next (voice/role),
    # then domain instructions, then the listings. The agent reads top-down.
    preamble = DEFAULT_INSTRUCTIONS if include_default_instructions else None
    parts = [
        p for p in (preamble, persona_prompt, instructions, action_surface, discovery_hint) if p
    ]
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
    # User-supplied middleware runs AFTER the built-in coercion middleware,
    # so by the time a user's TimingMiddleware / StructuredLoggingMiddleware /
    # RateLimitingMiddleware sees a tools/call, JSON-string args have already
    # been re-parsed. Add order matters for FastMCP middleware; this places
    # user code closer to the tool body.
    for _mw in middleware or ():
        mcp.add_middleware(_mw)

    # ── resources ────────────────────────────────────────────────────

    @mcp.resource("theodosia://graph")
    async def _graph_resource() -> str:
        """Static description of the Application's FSM topology.

        Read once per session. The graph doesn't change after mount;
        a model that has this resource doesn't need to keep polling
        ``theodosia://next`` to plan ahead. Each tool response already
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

    @mcp.resource("theodosia://state")
    async def _state_resource(ctx: Context) -> str:
        """Current Application state as JSON.

        Internal Burr keys (``__PRIOR_STEP``, ``__SEQUENCE_ID``) are
        filtered. Non-JSON-representable values are coerced to strings,
        with the affected keys listed under ``_theodosia.coerced_keys``
        so the client knows the round-trip is lossy.
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        state, coerced = _serializable_state(_public_state(app.state.get_all()))
        if coerced:
            state["_theodosia"] = {"coerced_keys": coerced}
        return json.dumps(state, indent=2)

    @mcp.resource("theodosia://next")
    async def _next_resource(ctx: Context) -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        return json.dumps(valid_next_action_names(app))

    @mcp.resource("theodosia://history")
    async def _history_resource(ctx: Context) -> str:
        """Timeline of every action attempted in this session.

        The payload is a JSON array (no wrapper object) of entries each
        carrying ``seq``, ``ts``, ``action``, ``inputs``, ``state_after``,
        ``valid_next_actions``, ``refused``, and ``refusal_reason``. Both
        successful steps and refused attempts (invalid transitions, unknown
        actions) appear. In factory-mode deployments each session sees only
        its own history; in shared-app deployments each session sees the
        timeline of its own calls against the shared FSM.
        """
        history = store.history(ctx.session_id) if ctx is not None else []
        return json.dumps(history, default=str, indent=2)

    @mcp.resource("theodosia://subruns")
    async def _subruns_resource(ctx: Context) -> str:
        """Index of sub-Application runs spawned in this session.

        Each entry has ``id``, ``uri``, ``label``, ``started_ts``,
        ``ended_ts``, and the ``parent_action`` that spawned it. The
        ``uri`` field is the fully-rendered ``theodosia://subruns/{id}``
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
                    "uri": f"theodosia://subruns/{sid}",
                    "label": record.get("label"),
                    "started_ts": record.get("started_ts"),
                    "ended_ts": record.get("ended_ts"),
                    "parent_action": parent_action_for.get(sid),
                    "error": record.get("error"),
                }
            )
        return json.dumps(index, default=str, indent=2)

    @mcp.resource("theodosia://subruns/{subrun_id}")
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

    @mcp.resource("theodosia://trace")
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

        This is the cross-reference between theodosia's in-memory
        ``theodosia://history`` (one entry per attempted action, including
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
                        "the Application to enable theodosia://trace."
                    ),
                },
                indent=2,
            )
        if not path.exists():
            return json.dumps([])
        return json.dumps(_read_trace(path), default=str, indent=2)

    @mcp.resource("theodosia://session")
    async def _session_resource(ctx: Context) -> str:
        """Tracker coordinates for the current MCP session's Application.

        Returns ``{project, app_id, app_dir, partition_key}`` so a client
        (or the agent itself) can locate this session's tracker data on
        disk without guessing. Useful for terminal tooling like
        ``theodosia watch <project>`` that tails the LocalTrackingClient
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
            LocalTrackingClient = None  # type: ignore[assignment,misc]
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

    # ── personas: identity layer (when mounted with personas=) ──────
    if personas_map:

        @mcp.resource("theodosia://personas")
        async def _personas_resource() -> str:
            """Index of personas mounted on this server.

            Returns ``{name: {description, voice, has_metadata}}`` so a client
            can list available identities without fetching every body. Each
            persona is also registered as an MCP prompt named
            ``theodosia/persona/<name>``; ``prompts/get`` returns the body.
            """
            return json.dumps(
                {
                    p.name: {
                        "description": p.description,
                        "voice": p.voice,
                        "has_metadata": bool(p.metadata),
                    }
                    for p in personas_map.values()
                },
                indent=2,
            )

        if persona is not None:
            default_name = persona.name

            @mcp.resource("theodosia://persona")
            async def _active_persona_resource() -> str:
                """The persona currently active for this server.

                For v0.3 the active persona is whichever was chosen at mount
                time via ``default_persona=`` (or the lexically first one).
                Mid-session swap via a ``set_persona`` tool is planned for
                v0.3.1 and will make this per-session.
                """
                p = personas_map[default_name]
                return json.dumps(
                    {
                        "name": p.name,
                        "description": p.description,
                        "voice": p.voice,
                        "body": p.body,
                        "metadata": p.metadata,
                    },
                    indent=2,
                )

        # Each persona becomes an MCP prompt. ``ctx: Context`` is the
        # FastMCP convention for server-injected context (without the
        # annotation it would be advertised as a required client argument).
        # When a session is active the body is interpolated against the
        # current frame; unknown placeholders render as empty strings.
        for _persona in personas_map.values():
            _name = f"theodosia/persona/{_persona.name}"
            _desc = _persona.description or f"Persona: {_persona.name}"

            def _make_prompt_fn(persona_obj):
                async def _persona_prompt_fn(ctx: Context) -> str:
                    frame = _build_persona_frame(ctx, store, factory)
                    return persona_obj.to_prompt_text(frame=frame)

                return _persona_prompt_fn

            mcp.prompt(name=_name, description=_desc)(_make_prompt_fn(_persona))

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
                    consult ``theodosia://next`` and the action's docstring
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
                # A hallucinated action name is the refusal a weaker model
                # is most likely to hit. Steer it the same way an
                # invalid_transition does: resolve the session's current
                # Application, report what's reachable now, and run the
                # reactive-hint path so the response is self-correcting on
                # its own. ``known_actions`` stays for spotting typos.
                app, _, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
                valid = valid_next_action_names(app)
                response: dict[str, Any] = {
                    "error": "unknown_action",
                    "requested": action,
                    "known_actions": action_names,
                    "valid_next_actions": valid,
                    "message": (
                        f"unknown action {action!r}. Reachable actions from the "
                        f"current state: {valid}."
                    ),
                }
                state_for_hint, _ = _serializable_state(_public_state(app.state.get_all()))
                hint = _compose_next_hint(
                    state=state_for_hint,
                    valid_next=valid,
                    last_action=action,
                    refusal=response,
                    domain_callback=next_hint,
                )
                if hint:
                    response = response | {"next_hint": hint}
                if external_tools_map:
                    net = _next_external_tools(external_tools_map, valid)
                    if net:
                        response = response | {"next_external_tools": net}
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    valid_next_actions=valid,
                    refused=True,
                    refusal_reason="unknown_action",
                    app=app,
                )
                headline = f"Step {seq}: {action} ✗ unknown_action"
                await _emit_log(ctx, headline)
                return _step_tool_result(response, headline)
            app, lock, entry = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
            effective_timeout = _action_timeout(action_map[action], action_timeout_seconds)
            effective_validator = _action_validator(action_map[action], input_validators)
            token = _current_session_entry.set(entry)
            ctx_token = _current_fastmcp_context.set(ctx)
            upstream_token = bind_upstream(upstream_manager) if upstream_manager else None
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
                # Reactive hint on refusal -- the FSM teaches the agent
                # why the call was blocked plus what's reachable now.
                state_for_hint, _ = _serializable_state(_public_state(app.state.get_all()))
                hint = _compose_next_hint(
                    state=state_for_hint,
                    valid_next=response.get("valid_next_actions") or [],
                    last_action=action,
                    refusal=response,
                    domain_callback=next_hint,
                )
                if hint:
                    response = response | {"next_hint": hint}
                if external_tools_map:
                    net = _next_external_tools(
                        external_tools_map, response.get("valid_next_actions") or []
                    )
                    if net:
                        response = response | {"next_external_tools": net}
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    app=app,
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
                if upstream_token is not None:
                    reset_upstream(upstream_token)
            new_subruns: list[str] = []
            if entry is not None:
                new_subruns = [s for s in entry.subruns if s not in subruns_before]
            # Reactive hint on success -- FSM-derived guidance for the
            # next move. Auto-hint enumerates reachable actions; the
            # domain callback can override with semantic-rich guidance.
            hint = _compose_next_hint(
                state=out["state"],
                valid_next=out["valid_next_actions"],
                last_action=action,
                refusal=None,
                domain_callback=next_hint,
            )
            if hint:
                out = out | {"next_hint": hint}
            if external_tools_map:
                net = _next_external_tools(external_tools_map, out["valid_next_actions"])
                if net:
                    out = out | {"next_external_tools": net}
            _record_history(
                store,
                ctx,
                factory,
                action=action,
                inputs=inputs or {},
                state_after=out["state"],
                valid_next_actions=out["valid_next_actions"],
                subruns=new_subruns or None,
                app=app,
            )
            headline = _success_headline(seq, action, out["valid_next_actions"])
            await _emit_log(ctx, headline)
            return _step_tool_result(out, headline)

        # Constrain the action parameter to the actual graph's action names so
        # the tool schema advertises an enum. Weak models otherwise hallucinate
        # plausible-sounding action names (e.g. "Start the workflow.") and
        # never recover; verified in the floor test (qwen3:0.6b went from 0/10
        # to 10/10 once the enum was present). The injection is via Pydantic's
        # Field(json_schema_extra=...) on an Annotated wrapper so FastMCP's
        # schema generator picks it up without us forking the type hint.
        step.__annotations__["action"] = Annotated[
            str,
            pydantic.Field(
                description=(
                    "Name of the action to run. Must be one of the listed "
                    "values; calling an out-of-state value returns an "
                    "invalid_transition error with the current valid set."
                ),
                json_schema_extra={"enum": action_names.copy()},  # type: ignore[dict-item]
            ),
        ]
        from mcp.types import ToolAnnotations

        step_description = f"{step.__doc__}\n\n{action_surface}"
        mcp.tool(
            name="step",
            description=step_description,
            output_schema=_step_response_schema(),
            annotations=ToolAnnotations(
                title="Take one FSM transition",
                # Each call advances the session's state machine.
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
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
        marker entry to ``theodosia://history``. Prior history entries are
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
                new_state["_theodosia"] = {"coerced_keys": coerced}
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
            app=new_app,
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

    from mcp.types import ToolAnnotations

    mcp.tool(
        name="reset_session",
        description=reset_session.__doc__,
        annotations=ToolAnnotations(
            title="Reset this session",
            destructiveHint=True,  # discards the session's state and history
            idempotentHint=True,  # repeated resets are no-ops after the first
            openWorldHint=False,
        ),
    )(reset_session)

    # ── meta tool: fork_at ──────────────────────────────────────────
    # Rewind the session's Application to the state captured after a
    # specific history entry. Lets an agent explore "what if" branches
    # without disconnecting and losing context. Implemented via our
    # in-memory history rather than Burr's tracker-based replay so it
    # works without requiring users to wire up a LocalTrackingClient.

    async def fork_at(sequence_id: int, ctx: Context | None = None) -> ToolResult | dict[str, Any]:
        """Rewind the session to the state captured after history[seq=N].

        ``sequence_id`` is the ``seq`` field on a ``theodosia://history`` entry.
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
            app=new_app,
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

    mcp.tool(
        name="fork_at",
        description=fork_at.__doc__,
        annotations=ToolAnnotations(
            title="Branch this session from a prior step",
            destructiveHint=True,  # replaces current state with the past snapshot
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(fork_at)

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
            app=new_app,
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

    mcp.tool(
        name="fork_from_past",
        description=fork_from_past.__doc__,
        annotations=ToolAnnotations(
            title="Resume a different session's past state",
            destructiveHint=True,  # replaces this session's state with another's
            idempotentHint=False,
            openWorldHint=True,  # reaches outside the current session's history
        ),
    )(fork_from_past)

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
    hooks: list[Any] | None = None,
    middleware: list[Any] | None = None,
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
      ``theodosia://graph`` from app ``order`` becomes ``theodosia://order/graph``.

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
        hooks: Burr ``LifecycleAdapter`` instances forwarded to each
            sub-application's ``mount()`` call. Attached to every
            sub-application's sessions; if you need per-app hooks, mount
            them separately and compose manually.
        middleware: FastMCP ``Middleware`` instances forwarded to each
            sub-application's ``mount()`` call. Each sub-server's middleware
            sees the post-routing tool name (``step``, not ``<app>_step``)
            because FastMCP composition strips the namespace before
            dispatching to the sub-server. The parent server is not
            middleware-wrapped here; add middleware to the returned FastMCP
            with ``server.add_middleware(...)`` if you want a single
            wrapper around all sub-servers.

    A ``theodosia://apps`` resource on the parent lists the mounted app
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

    parent_name = name or "theodosia-multi"
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
        "theodosia://<app>/<path>. Read `theodosia://apps` for the live list."
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
            hooks=hooks,
            middleware=middleware,
        )
        parent.mount(sub, namespace=app_name)

    namespace_list = sorted(applications)

    @parent.resource("theodosia://apps")
    async def _apps_resource() -> str:
        """List the apps mounted on this multi-Application server."""
        return json.dumps({"apps": namespace_list}, indent=2)

    return parent
