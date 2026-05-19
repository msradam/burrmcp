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
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from enum import Enum
from typing import Any

from burr.core import Application
from burr.core.action import Action, Condition
from fastmcp import FastMCP


class ServingMode(str, Enum):
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
    """Return ``(required, optional)`` input names for an action.

    Burr's ``Action.inputs`` is ``(required, optional)`` already, but
    older actions may expose it as a flat list. Normalise here.
    """
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
    import typing

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
        # In tools mode the graph is advisory. If the requested action
        # isn't in the current valid-next set, run it bare against
        # current state via the action's own callable. Burr's tracker
        # is not invoked on this path; that's the trade-off tools mode
        # takes on.
        if action_name not in valid:
            from burr.core.state import State

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
    """Run an action against the current state without graph enforcement.

    Used by ``ServingMode.TOOLS`` when the requested action is not in
    the standard valid-next set. Bypasses Burr's transition machinery
    while still updating ``app.state``. Burr's tracker is not invoked
    on this path; tools mode opts into that trade-off.
    """
    state = app.state
    # ``is_async`` is a method on FunctionBasedAction; the async-ness of
    # the wrapped function is what matters here.
    import asyncio

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
    app: Application,
    action: Action,
    enforce_transitions: bool,
    refresh_tools_after: Callable[[], None] | None = None,
) -> Callable:
    """Build an async MCP tool handler for one Burr action.

    The handler's signature is dynamically constructed to match the
    action's declared inputs so FastMCP introspects the correct
    JSON Schema. Body delegates to ``_step_application``.
    """
    params = _action_signature_params(action)
    action_name = action.name

    async def handler(**kwargs: Any) -> dict[str, Any]:
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
        if refresh_tools_after is not None:
            refresh_tools_after()
        return out

    handler.__name__ = f"action_{action_name}"
    handler.__doc__ = (
        action.fn.__doc__
        if getattr(action, "fn", None) and action.fn.__doc__
        else f"Run the {action_name!r} action."
    )
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=params,
        return_annotation=dict,
    )
    # Pydantic's TypeAdapter (used by FastMCP to build the JSON schema)
    # reads ``__annotations__``, not ``__signature__``. Populate both.
    handler.__annotations__ = {p.name: p.annotation for p in params}
    handler.__annotations__["return"] = dict
    return handler


def _refresh_dynamic_visibility(mcp: FastMCP, app: Application) -> None:
    """For ``ServingMode.DYNAMIC``: enable tools for valid-next actions,
    disable the rest.

    Uses tag-based visibility. Every action tool is tagged with
    ``f"action:{name}"``; we enable the valid set and disable the
    others. FastMCP fires ``notifications/tools/list_changed`` when the
    visible set changes.
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
    application: Application,
    *,
    mode: ServingMode = ServingMode.STEP,
    name: str | None = None,
    instructions: str | None = None,
) -> FastMCP:
    """Return a FastMCP server that exposes ``application`` per ``mode``.

    The Application instance is captured by reference; state is mutated
    in place on each tool call. For per-session isolation, build a new
    Application per session at the FastMCP lifespan layer (see
    ``examples/per_session.py``).

    Args:
        application: A built Burr ``Application``.
        mode: One of ``ServingMode.TOOLS``, ``STEP``, or ``DYNAMIC``.
        name: MCP server name; defaults to ``"burr-mcp"``.
        instructions: Server-level instructions surfaced via the MCP
            spec's server-info ``instructions`` field.
    """
    server_name = name or "burr-mcp"
    mcp = FastMCP(server_name, instructions=instructions)

    # ── resources: state + valid-next actions ─────────────────────────

    @mcp.resource("burr://state")
    async def _state_resource() -> str:
        """Current Application state as JSON.

        Internal Burr keys (``__PRIOR_STEP``, ``__SEQUENCE_ID``) are
        filtered. Use this to inspect what the FSM has accumulated
        across tool calls.
        """
        return json.dumps(_public_state(application.state.get_all()), default=str, indent=2)

    @mcp.resource("burr://next")
    async def _next_resource() -> str:
        """Action names reachable from the current state.

        For non-branching graphs this is one name. For branching graphs,
        all conditionally-reachable next actions are listed. After a
        terminal action this is an empty list, meaning the FSM is done.
        """
        return json.dumps(valid_next_action_names(application))

    # ── tools, per mode ──────────────────────────────────────────────

    if mode is ServingMode.STEP:
        action_names = [a.name for a in application.graph.actions]
        action_map = {a.name: a for a in application.graph.actions}

        async def step(action: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
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
            try:
                return await _step_application(
                    application,
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
        for action in application.graph.actions:
            handler = _make_tool_handler(application, action, enforce_transitions=False)
            mcp.tool(name=action.name, description=handler.__doc__)(handler)

    elif mode is ServingMode.DYNAMIC:
        def _refresh() -> None:
            _refresh_dynamic_visibility(mcp, application)

        for action in application.graph.actions:
            handler = _make_tool_handler(
                application,
                action,
                enforce_transitions=True,
                refresh_tools_after=_refresh,
            )
            mcp.tool(
                name=action.name,
                description=handler.__doc__,
                tags={f"action:{action.name}"},
            )(handler)
        # Initial visibility: only the currently-valid actions are visible.
        _refresh()
    else:
        raise ValueError(f"unknown serving mode: {mode!r}")

    return mcp
