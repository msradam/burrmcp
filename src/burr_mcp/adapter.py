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

All modes register two resources:

  • ``burr://state``: current Application state as JSON.
  • ``burr://next``:  list of action names reachable from now.

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
import typing
from collections.abc import Callable
from enum import Enum
from typing import Any

from burr.core import Application
from burr.core.action import Action, Condition
from fastmcp import Context, FastMCP

ApplicationFactory = Callable[[], Application]
ApplicationOrFactory = Application | ApplicationFactory

# Key used to stash the session-scoped Application on FastMCP's context.
_SESSION_APP_KEY = "_burr_mcp_application"


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


def _session_app(
    ctx: Context | None,
    shared_app: Application,
    factory: ApplicationFactory | None,
    session_store: dict[str, Application],
) -> Application:
    """Resolve the Application for this request.

    When ``factory`` is None, returns ``shared_app`` (the mount-time
    instance, shared across sessions). When ``factory`` is set, looks
    up the session-scoped Application by ``ctx.session_id`` in the
    server's own session store, creating one via the factory if this
    is the session's first call.

    The store is a plain dict held in closure scope by ``mount(...)``.
    FastMCP's own ``ctx.set_state(serializable=False)`` is request-scoped
    rather than session-scoped, so it isn't suitable for caching the
    Application across multiple tool calls in one session. Entries are
    not cleaned up when sessions end; for long-running servers with many
    short sessions, an eviction policy should be added (v0.2).

    ``ctx`` may be None when invoked outside an MCP request (e.g. the
    initial dynamic-mode visibility refresh). In that case the shared
    app is returned even in factory mode; the refresh is a best-effort
    signal that operates on the template's graph shape.
    """
    if factory is None or ctx is None:
        return shared_app
    sid = ctx.session_id
    existing = session_store.get(sid)
    if existing is None:
        existing = factory()
        session_store[sid] = existing
    return existing


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
    session_store: dict[str, Application],
    action: Action,
    enforce_transitions: bool,
    refresh_tools_after: Callable[[FastMCP, Application], None] | None = None,
    mcp_server: FastMCP | None = None,
) -> Callable:
    """Build an async MCP tool handler for one Burr action.

    The handler's signature is dynamically constructed to match the
    action's declared inputs so FastMCP introspects the correct
    JSON Schema. Body resolves the session-scoped Application and
    delegates to ``_step_application``.
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
        app = _session_app(ctx, shared_app, factory, session_store)
        try:
            out = await _step_application(
                app,
                action_name=action_name,
                inputs=kwargs,
                enforce_transitions=enforce_transitions,
            )
        except InvalidTransitionError as e:
            return {
                "error": "invalid_transition",
                "requested": e.requested,
                "valid_next_actions": e.valid,
                "message": str(e),
            }
        if refresh_tools_after is not None and mcp_server is not None:
            refresh_tools_after(mcp_server, app)
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


def _refresh_dynamic_visibility(mcp: FastMCP, app: Application) -> None:
    """For ``ServingMode.DYNAMIC``: enable tools for valid-next actions,
    disable the rest.

    Uses tag-based visibility. Every action tool is tagged with
    ``f"action:{name}"``; this enables the valid set and disables the
    others. FastMCP fires ``notifications/tools/list_changed`` when the
    visible set changes. As of FastMCP 3.2, enable/disable transforms
    are per-session, so concurrent sessions see independent visibility.
    """
    valid = set(valid_next_action_names(app))
    all_action_names = {a.name for a in app.graph.actions}
    enable_tags = {f"action:{n}" for n in valid}
    disable_tags = {f"action:{n}" for n in all_action_names - valid}
    if disable_tags:
        mcp.disable(tags=disable_tags)
    if enable_tags:
        mcp.enable(tags=enable_tags)


def mount(
    application: ApplicationOrFactory,
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
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
    """
    shared_app, factory = _resolve(application)
    # Per-session store. Keyed by ctx.session_id; populated lazily on
    # the first tool call of each session. Lives in closure scope so
    # it's tied to this server instance, not module-global.
    session_store: dict[str, Application] = {}

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
        app = _session_app(ctx, shared_app, factory, session_store)
        return json.dumps(_public_state(app.state.get_all()), default=str, indent=2)

    @mcp.resource("burr://next")
    async def _next_resource(ctx: Context) -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        app = _session_app(ctx, shared_app, factory, session_store)
        return json.dumps(valid_next_action_names(app))

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
                return {
                    "error": "unknown_action",
                    "requested": action,
                    "known_actions": action_names,
                }
            app = _session_app(ctx, shared_app, factory, session_store)
            try:
                return await _step_application(
                    app,
                    action_name=action,
                    inputs=inputs or {},
                    enforce_transitions=True,
                )
            except InvalidTransitionError as e:
                return {
                    "error": "invalid_transition",
                    "requested": e.requested,
                    "valid_next_actions": e.valid,
                    "message": str(e),
                }

        mcp.tool(name="step", description=step.__doc__)(step)

    elif mode is ServingMode.TOOLS:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                factory=factory,
                session_store=session_store,
                action=action,
                enforce_transitions=False,
            )
            mcp.tool(name=action.name, description=handler.__doc__)(handler)

    elif mode is ServingMode.DYNAMIC:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                factory=factory,
                session_store=session_store,
                action=action,
                enforce_transitions=True,
                refresh_tools_after=_refresh_dynamic_visibility,
                mcp_server=mcp,
            )
            mcp.tool(
                name=action.name,
                description=handler.__doc__,
                tags={f"action:{action.name}"},
            )(handler)
        _refresh_dynamic_visibility(mcp, shared_app)
    else:
        raise ValueError(f"unknown serving mode: {mode!r}")

    return mcp
