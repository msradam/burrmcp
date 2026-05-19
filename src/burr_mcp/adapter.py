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

All modes register three resources:

  • ``burr://state``:   current Application state as JSON.
  • ``burr://next``:    list of action names reachable from now.
  • ``burr://history``: per-session timeline of every action attempt
                        (successes + refusals), each with timestamp,
                        inputs, resulting state, and valid-next set.

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
    """

    application: Application | None
    history: list[dict[str, Any]] = field(default_factory=list)
    last_access: float = field(default_factory=time.monotonic)


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
) -> None:
    """Append one timeline entry to this session's history.

    Records successes and refusals alike. No-op when ``ctx`` is None
    (calls outside an MCP request, e.g. the initial dynamic-mode
    visibility refresh).
    """
    if ctx is None:
        return
    entry = store.get_or_create(ctx.session_id, factory)
    entry.history.append(
        {
            "seq": len(entry.history),
            "ts": datetime.now(UTC).isoformat(),
            "action": action,
            "inputs": inputs,
            "state_after": state_after,
            "valid_next_actions": valid_next_actions,
            "refused": refused,
            "refusal_reason": refusal_reason,
        }
    )
    entry.last_access = time.monotonic()


def _session_app(
    ctx: Context | None,
    shared_app: Application,
    factory: ApplicationFactory | None,
    store: _SessionStore,
) -> Application:
    """Resolve the Application for this request.

    Shared-app mode (factory is None): returns ``shared_app`` regardless
    of session id. Factory mode: looks up the session entry in ``store``,
    creating a new Application via the factory on first access.

    The store backs both Application caching and history recording.
    Lazy TTL + max-size eviction lives on the store itself. FastMCP's
    own ``ctx.set_state(serializable=False)`` is request-scoped rather
    than session-scoped, so it's not suitable for caching across calls
    in one session.

    ``ctx`` is None when invoked outside an MCP request (e.g. the
    initial dynamic-mode visibility refresh at mount time). In that
    case the shared app is returned even in factory mode; the refresh
    operates on the template's graph shape.
    """
    if factory is None or ctx is None:
        return shared_app
    entry = store.get_or_create(ctx.session_id, factory)
    assert entry.application is not None  # factory mode guarantees this
    return entry.application


async def _step_application(
    app: Application,
    action_name: str,
    inputs: dict[str, Any],
    enforce_transitions: bool,
) -> dict[str, Any]:
    """Run one step of the Application.

    When ``enforce_transitions`` is True, refuses to step unless
    ``action_name`` is in the current valid-next set. When False
    (``ServingMode.TOOLS``), runs the action directly against current
    state, bypassing Burr's transition machinery.
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
            return await _run_action_bare(app, action_obj, inputs)

    a, result, new_state = await app.astep(inputs=inputs)
    return {
        "action": a.name,
        "result": result,
        "state": _public_state(new_state.get_all()),
        "valid_next_actions": valid_next_action_names(app),
    }


async def _run_action_bare(
    app: Application,
    action_obj: Action,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Run an action against current state without graph enforcement.

    Used by ``ServingMode.TOOLS`` when the requested action is not in
    the standard valid-next set. Bypasses Burr's transition machinery
    while still updating ``app.state``. Burr's tracker is not invoked
    on this path; tools mode opts into that trade-off.
    """
    state = app.state
    raw = action_obj.fn(state, **inputs)
    new_state = await raw if asyncio.iscoroutine(raw) else raw
    app.update_state(new_state)
    return {
        "action": action_obj.name,
        "result": {},
        "state": _public_state(app.state.get_all()),
        "valid_next_actions": valid_next_action_names(app),
        "note": "ran without transition enforcement (tools mode)",
    }


def _make_tool_handler(
    shared_app: Application,
    factory: ApplicationFactory | None,
    store: _SessionStore,
    action: Action,
    enforce_transitions: bool,
    refresh_session_visibility: bool = False,
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
        app = _session_app(ctx, shared_app, factory, store)
        try:
            out = await _step_application(
                app,
                action_name=action_name,
                inputs=kwargs,
                enforce_transitions=enforce_transitions,
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

    server_name = name or "burr-mcp"
    mcp = FastMCP(server_name, instructions=instructions)

    # ── resources ────────────────────────────────────────────────────

    @mcp.resource("burr://state")
    async def _state_resource(ctx: Context) -> str:
        """Current Application state as JSON.

        Internal Burr keys (``__PRIOR_STEP``, ``__SEQUENCE_ID``) are
        filtered. Use this to inspect what the FSM has accumulated
        across tool calls.
        """
        app = _session_app(ctx, shared_app, factory, store)
        return json.dumps(_public_state(app.state.get_all()), default=str, indent=2)

    @mcp.resource("burr://next")
    async def _next_resource(ctx: Context) -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        app = _session_app(ctx, shared_app, factory, store)
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
            app = _session_app(ctx, shared_app, factory, store)
            try:
                out = await _step_application(
                    app,
                    action_name=action,
                    inputs=inputs or {},
                    enforce_transitions=True,
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
                factory=factory,
                store=store,
                action=action,
                enforce_transitions=False,
            )
            mcp.tool(name=action.name, description=handler.__doc__)(handler)

    elif mode is ServingMode.DYNAMIC:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                factory=factory,
                store=store,
                action=action,
                enforce_transitions=True,
                refresh_session_visibility=True,
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
