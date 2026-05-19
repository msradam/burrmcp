"""Mount a Burr Application as a FastMCP server.

Three serving modes:

  • ``ServingMode.TOOLS``:   Each ``@action`` exposed as its own MCP
                             tool. No transition enforcement; state
                             mutated freely. Closest analogue to a flat
                             MCP server today.
  • ``ServingMode.STEP``:    One ``step(action_name, **inputs)``
                             meta-tool. Server enforces valid
                             transitions. Works on every MCP client,
                             including clients that ignore
                             ``notifications/tools/list_changed``.
                             Default.
  • ``ServingMode.DYNAMIC``: Per-action tools whose visibility tracks
                             current state via tags. Sends
                             ``tools/list_changed`` after each step.
                             Best when the client honors it.

All modes register four resources:

  • ``burr://state``:   current Application state as JSON.
  • ``burr://next``:    list of action names reachable from now.
  • ``burr://history``: per-session timeline of every action attempt
                        (successes + refusals), each with timestamp,
                        inputs, resulting state, and valid-next set.
  • ``burr://trace``:   Burr's on-disk LocalTrackingClient log for the
                        current session's Application. Empty if no
                        tracker attached. Cross-reference for full
                        Burr replay format.

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
import inspect
import json
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from burr.core import Application
from burr.core.action import Action, Condition
from fastmcp import Context, FastMCP

ApplicationFactory = Callable[[], Application]
ApplicationOrFactory = Application | ApplicationFactory

# Defaults for session-store eviction.
_DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour idle
_DEFAULT_MAX_SESSIONS = 100


class ServingMode(str, Enum):  # noqa: UP042  # leaving as (str, Enum) for stable wire serialization across Python versions
    TOOLS = "tools"
    STEP = "step"
    DYNAMIC = "dynamic"


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
    keys is surfaced to the client via ``_burr_mcp.coerced_keys`` on
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

    params: list[inspect.Parameter] = []
    for name in required:
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=_annotation_for(name),
            )
        )
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
    """

    application: Application | None
    history: list[dict[str, Any]] = field(default_factory=list)
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
) -> None:
    """Append one timeline entry to this session's history.

    Records successes and refusals alike. When ``refusal_reason`` is
    ``"action_error"``, the entry also carries ``error_message`` and
    ``error_type`` so a client can distinguish "the FSM said no" from
    "the action's code raised." No-op when ``ctx`` is None (calls
    outside an MCP request, e.g. the initial dynamic-mode visibility
    refresh).
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
    entry.history.append(record)
    entry.last_access = time.monotonic()


def _session_app_and_lock(
    ctx: Context | None,
    shared_app: Application,
    shared_lock: asyncio.Lock,
    factory: ApplicationFactory | None,
    store: _SessionStore,
) -> tuple[Application, asyncio.Lock]:
    """Resolve the (Application, lock) pair for this request.

    Shared-app mode (factory is None): returns ``shared_app`` plus the
    server-wide ``shared_lock``. All sessions serialise their step
    calls on this lock, because they're all mutating one Application.

    Factory mode: returns the session's own Application and the
    session entry's own lock. Different sessions' steps run in
    parallel; calls within one session queue on its lock.

    ``ctx`` may be None when invoked outside an MCP request (e.g. the
    initial dynamic-mode visibility refresh). In that case we fall back
    to ``shared_app``/``shared_lock`` even in factory mode; the refresh
    operates on the template's graph shape.
    """
    if factory is None or ctx is None:
        return shared_app, shared_lock
    entry = store.get_or_create(ctx.session_id, factory)
    assert entry.application is not None  # factory mode guarantees this
    return entry.application, entry.lock


async def _step_application(
    app: Application,
    action_name: str,
    inputs: dict[str, Any],
    enforce_transitions: bool,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run one step of the Application.

    When ``enforce_transitions`` is True, refuses to step unless
    ``action_name`` is in the current valid-next set. When False
    (``ServingMode.TOOLS``), runs the action directly against current
    state, bypassing Burr's transition machinery.

    When ``timeout_seconds`` is set, the call is wrapped in
    ``asyncio.wait_for``; on expiry the action's coroutine is cancelled
    and ``ActionTimeoutError`` is raised. None means no timeout.

    Exceptions raised by the action's wrapped function are caught and
    re-raised as ``ActionExecutionError`` so callers can record them
    structurally in the session's history.
    """
    valid = valid_next_action_names(app)
    if enforce_transitions:
        if action_name not in valid:
            raise InvalidTransitionError(action_name, valid)
    else:
        if action_name not in valid:
            action_obj = app.graph.get_action(action_name)
            if action_obj is None:
                raise InvalidTransitionError(action_name, valid)
            return await _run_action_bare(app, action_obj, inputs, timeout_seconds)

    try:
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
    state, coerced = _serializable_state(_public_state(new_state.get_all()))
    if coerced:
        state["_burr_mcp"] = {"coerced_keys": coerced}
    return {
        "action": a.name,
        "result": result,
        "state": state,
        "valid_next_actions": valid_next_action_names(app),
    }


async def _run_action_bare(
    app: Application,
    action_obj: Action,
    inputs: dict[str, Any],
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run an action against current state without graph enforcement.

    Used by ``ServingMode.TOOLS`` when the requested action is not in
    the standard valid-next set. Bypasses Burr's transition machinery
    while still updating ``app.state``. Burr's tracker is not invoked
    on this path; tools mode opts into that trade-off.

    Exceptions raised by the wrapped function are caught and re-raised
    as ``ActionExecutionError`` (same shape as ``_step_application``).
    Honours ``timeout_seconds`` if set, raising ``ActionTimeoutError``
    on expiry.
    """
    state = app.state
    try:
        raw = action_obj.fn(state, **inputs)
        if asyncio.iscoroutine(raw):
            if timeout_seconds is not None:
                new_state = await asyncio.wait_for(raw, timeout=timeout_seconds)
            else:
                new_state = await raw
        else:
            new_state = raw
    except TimeoutError as exc:
        raise ActionTimeoutError(action_obj.name, timeout_seconds or 0.0) from exc
    except Exception as exc:
        raise ActionExecutionError(action_obj.name, exc) from exc
    app.update_state(new_state)
    out_state, coerced = _serializable_state(_public_state(app.state.get_all()))
    if coerced:
        out_state["_burr_mcp"] = {"coerced_keys": coerced}
    return {
        "action": action_obj.name,
        "result": {},
        "state": out_state,
        "valid_next_actions": valid_next_action_names(app),
        "note": "ran without transition enforcement (tools mode)",
    }


def _make_tool_handler(
    shared_app: Application,
    shared_lock: asyncio.Lock,
    factory: ApplicationFactory | None,
    store: _SessionStore,
    action: Action,
    enforce_transitions: bool,
    refresh_session_visibility: bool = False,
    timeout_seconds: float | None = None,
) -> Callable:
    """Build an async MCP tool handler for one Burr action.

    The handler's signature is dynamically constructed to match the
    action's declared inputs so FastMCP introspects the correct
    JSON Schema. Body resolves the session-scoped Application,
    delegates to ``_step_application``, and records the attempt in
    the session's history.
    """
    params = _action_signature_params(action)
    # FastMCP injects a Context when the handler signature includes one.
    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Context,
        default=None,
    )
    action_name = action.name

    async def handler(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs.pop("ctx", None)
        app, lock = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        try:
            async with lock:
                out = await _step_application(
                    app,
                    action_name=action_name,
                    inputs=kwargs,
                    enforce_transitions=enforce_transitions,
                    timeout_seconds=timeout_seconds,
                )
        except InvalidTransitionError as e:
            _record_history(
                store,
                ctx,
                factory,
                action=action_name,
                inputs=kwargs,
                state_after=None,
                valid_next_actions=e.valid,
                refused=True,
                refusal_reason="invalid_transition",
            )
            return {
                "error": "invalid_transition",
                "requested": e.requested,
                "valid_next_actions": e.valid,
                "message": str(e),
            }
        except ActionTimeoutError as e:
            _record_history(
                store,
                ctx,
                factory,
                action=action_name,
                inputs=kwargs,
                state_after=None,
                valid_next_actions=valid_next_action_names(app),
                refused=True,
                refusal_reason="action_timeout",
                error_message=str(e),
                error_type="TimeoutError",
            )
            return {
                "error": "action_timeout",
                "requested": action_name,
                "timeout_seconds": e.timeout_seconds,
                "message": str(e),
                "valid_next_actions": valid_next_action_names(app),
            }
        except ActionExecutionError as e:
            _record_history(
                store,
                ctx,
                factory,
                action=action_name,
                inputs=kwargs,
                state_after=None,
                valid_next_actions=valid_next_action_names(app),
                refused=True,
                refusal_reason="action_error",
                error_message=str(e.original),
                error_type=type(e.original).__name__,
            )
            return {
                "error": "action_error",
                "requested": action_name,
                "error_type": type(e.original).__name__,
                "error_message": str(e.original),
                "valid_next_actions": valid_next_action_names(app),
            }
        _record_history(
            store,
            ctx,
            factory,
            action=action_name,
            inputs=kwargs,
            state_after=out["state"],
            valid_next_actions=out["valid_next_actions"],
        )
        if refresh_session_visibility and ctx is not None:
            await _refresh_session_dynamic_visibility(ctx, app)
        return out

    handler.__name__ = f"action_{action_name}"
    handler.__doc__ = (
        action.fn.__doc__
        if getattr(action, "fn", None) and action.fn.__doc__
        else f"Run the {action_name!r} action."
    )
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[*params, ctx_param],
        return_annotation=dict,
    )
    handler.__annotations__ = {p.name: p.annotation for p in params}
    handler.__annotations__["ctx"] = Context
    handler.__annotations__["return"] = dict
    return handler


def _refresh_global_dynamic_visibility(mcp: FastMCP, app: Application) -> None:
    """Set the server-wide baseline visibility for DYNAMIC mode.

    Used once at mount time so that fresh sessions see only the
    entrypoint actions before they've made any calls. Subsequent
    refreshes happen per-session via ``_refresh_session_dynamic_visibility``,
    and FastMCP's per-session rules override the global baseline.
    """
    valid = set(valid_next_action_names(app))
    all_action_names = {a.name for a in app.graph.actions}
    enable_tags = {f"action:{n}" for n in valid}
    disable_tags = {f"action:{n}" for n in all_action_names - valid}
    if disable_tags:
        mcp.disable(tags=disable_tags)
    if enable_tags:
        mcp.enable(tags=enable_tags)


async def _refresh_session_dynamic_visibility(ctx: Context, app: Application) -> None:
    """For ``ServingMode.DYNAMIC``: per-session visibility refresh.

    Uses ``ctx.enable_components`` / ``ctx.disable_components``, which
    apply only to the current MCP session and override the server-wide
    baseline. Sends a ``tools/list_changed`` notification to this
    session only. Concurrent sessions see independent visibility.
    """
    valid = set(valid_next_action_names(app))
    all_action_names = {a.name for a in app.graph.actions}
    enable_tags = {f"action:{n}" for n in valid}
    disable_tags = {f"action:{n}" for n in all_action_names - valid}
    if disable_tags:
        await ctx.disable_components(tags=disable_tags)
    if enable_tags:
        await ctx.enable_components(tags=enable_tags)


def mount(
    application: ApplicationOrFactory,
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
    session_ttl_seconds: int | None = _DEFAULT_SESSION_TTL_SECONDS,
    max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    action_timeout_seconds: float | None = None,
) -> FastMCP:
    """Return a FastMCP server that exposes ``application`` per ``mode``.

    Args:
        application: Either a built ``burr.core.Application`` (shared
            across all sessions) or a callable ``() -> Application``
            (called once per session for state isolation). The graph
            shape is read once at mount time, so factories should
            return Applications with the same graph each call.
        mode: One of ``ServingMode.TOOLS``, ``STEP``, or ``DYNAMIC``.
        name: MCP server name; defaults to ``"burr-mcp"``.
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
    # Lock used in shared-app mode (one Application, many sessions). In
    # factory mode each session has its own lock on its session entry,
    # so this one is only touched outside an MCP request (no concurrent
    # use) and for tools-mode ``_run_action_bare`` paths against the
    # template. Kept on the closure either way.
    shared_lock = asyncio.Lock()

    server_name = name or "burr-mcp"
    mcp = FastMCP(server_name, instructions=instructions)

    # ── resources ────────────────────────────────────────────────────

    @mcp.resource("burr://state")
    async def _state_resource(ctx: Context) -> str:
        """Current Application state as JSON.

        Internal Burr keys (``__PRIOR_STEP``, ``__SEQUENCE_ID``) are
        filtered. Non-JSON-representable values are coerced to strings,
        with the affected keys listed under ``_burr_mcp.coerced_keys``
        so the client knows the round-trip is lossy.
        """
        app, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        state, coerced = _serializable_state(_public_state(app.state.get_all()))
        if coerced:
            state["_burr_mcp"] = {"coerced_keys": coerced}
        return json.dumps(state, indent=2)

    @mcp.resource("burr://next")
    async def _next_resource(ctx: Context) -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        app, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
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

        This is the cross-reference between burr-mcp's in-memory
        ``burr://history`` (one entry per attempted action, including
        refusals) and Burr's own structured trace format (one entry
        per state transition, full Burr replay shape).
        """
        app, _ = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
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

    # ── tools, per mode ──────────────────────────────────────────────

    if mode is ServingMode.STEP:
        action_names = [a.name for a in shared_app.graph.actions]
        action_map = {a.name: a for a in shared_app.graph.actions}

        async def step(
            action: str,
            inputs: dict[str, Any] | None = None,
            ctx: Context = None,
        ) -> dict[str, Any]:
            """Advance the FSM by one transition.

            Args:
                action: Name of the action to run. Must be in the
                    current valid-next set; otherwise the call returns
                    an ``invalid_transition`` error with the list of
                    actions actually allowed right now.
                inputs: Keyword inputs to the action. Each action
                    declares its own required + optional inputs;
                    consult ``burr://next`` and the action's docstring
                    to see what's expected.
            """
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
                return {
                    "error": "unknown_action",
                    "requested": action,
                    "known_actions": action_names,
                }
            app, lock = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
            try:
                async with lock:
                    out = await _step_application(
                        app,
                        action_name=action,
                        inputs=inputs or {},
                        enforce_transitions=True,
                        timeout_seconds=action_timeout_seconds,
                    )
            except InvalidTransitionError as e:
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    valid_next_actions=e.valid,
                    refused=True,
                    refusal_reason="invalid_transition",
                )
                return {
                    "error": "invalid_transition",
                    "requested": e.requested,
                    "valid_next_actions": e.valid,
                    "message": str(e),
                }
            except ActionTimeoutError as e:
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    valid_next_actions=valid_next_action_names(app),
                    refused=True,
                    refusal_reason="action_timeout",
                    error_message=str(e),
                    error_type="TimeoutError",
                )
                return {
                    "error": "action_timeout",
                    "requested": action,
                    "timeout_seconds": e.timeout_seconds,
                    "message": str(e),
                    "valid_next_actions": valid_next_action_names(app),
                }
            except ActionExecutionError as e:
                _record_history(
                    store,
                    ctx,
                    factory,
                    action=action,
                    inputs=inputs or {},
                    state_after=None,
                    valid_next_actions=valid_next_action_names(app),
                    refused=True,
                    refusal_reason="action_error",
                    error_message=str(e.original),
                    error_type=type(e.original).__name__,
                )
                return {
                    "error": "action_error",
                    "requested": action,
                    "error_type": type(e.original).__name__,
                    "error_message": str(e.original),
                    "valid_next_actions": valid_next_action_names(app),
                }
            _record_history(
                store,
                ctx,
                factory,
                action=action,
                inputs=inputs or {},
                state_after=out["state"],
                valid_next_actions=out["valid_next_actions"],
            )
            return out

        mcp.tool(name="step", description=step.__doc__)(step)

    elif mode is ServingMode.TOOLS:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                shared_lock=shared_lock,
                factory=factory,
                store=store,
                action=action,
                enforce_transitions=False,
                timeout_seconds=action_timeout_seconds,
            )
            mcp.tool(name=action.name, description=handler.__doc__)(handler)

    elif mode is ServingMode.DYNAMIC:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                shared_lock=shared_lock,
                factory=factory,
                store=store,
                action=action,
                enforce_transitions=True,
                refresh_session_visibility=True,
                timeout_seconds=action_timeout_seconds,
            )
            mcp.tool(
                name=action.name,
                description=handler.__doc__,
                tags={f"action:{action.name}"},
            )(handler)
        # Server-wide baseline: only entrypoint visible to a fresh
        # session before its first call. Per-session refreshes after
        # each call override this baseline for that session only.
        _refresh_global_dynamic_visibility(mcp, shared_app)
    else:
        raise ValueError(f"unknown serving mode: {mode!r}")

    return mcp
