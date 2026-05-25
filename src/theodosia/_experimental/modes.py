"""TOOLS and DYNAMIC serving modes, carved out of ``adapter.py``.

Provenance: present in ``adapter.py`` through commits up to the
"strip TOOLS/DYNAMIC" change. STEP became the sole product; these
two modes were stashed here verbatim rather than deleted in case
the use case returns.

When active, ``mount()`` dispatched on ``ServingMode``:

* ``STEP``: one ``step(action, inputs)`` meta-tool with server-side
  transition enforcement. The product surface.
* ``TOOLS``: one MCP tool per ``@action`` with NO transition
  enforcement (``enforce_transitions=False``). For "flat" servers
  whose clients don't care about state machines, or for clients
  that can't be taught to call ``step``.
* ``DYNAMIC``: one MCP tool per ``@action`` WITH transition
  enforcement, and per-session ``tools/list_changed`` visibility
  tracking the current valid-next set. Needs a client that honors
  ``tools/list_changed`` (Claude Code as of mid-2026 does not;
  Cursor does not refresh on its own).

The mount() dispatch block looked like::

    if mode is ServingMode.STEP:
        ...  # current adapter.py body
    elif mode is ServingMode.TOOLS:
        for action in shared_app.graph.actions:
            handler = _make_tool_handler(
                shared_app=shared_app,
                shared_lock=shared_lock,
                factory=factory,
                store=store,
                action=action,
                enforce_transitions=False,
                timeout_seconds=_action_timeout(action, action_timeout_seconds),
                validator=_action_validator(action, input_validators),
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
                timeout_seconds=_action_timeout(action, action_timeout_seconds),
                validator=_action_validator(action, input_validators),
            )
            mcp.tool(
                name=action.name,
                description=handler.__doc__,
                tags={f"action:{action.name}"},
            )(handler)
        _refresh_global_dynamic_visibility(mcp, shared_app)
        mcp.add_middleware(
            _make_dynamic_refusal_middleware(shared_app, shared_lock, factory, store)
        )

To revive: copy the dispatch block back into ``mount()``, restore
``ServingMode.TOOLS`` and ``ServingMode.DYNAMIC`` to the enum,
restore the ``enforce_transitions`` parameter to ``_step_application``
(plus the bare-run branch), and re-import these helpers. The
helpers reference ``_session_app_and_lock``, ``valid_next_action_names``,
``_step_application``, the exception classes, ``_record_history``,
``_action_signature_params``, ``_serializable_state``,
``_public_state``, ``_tracker_project``, ``_run_validator``, and
``_current_session_entry`` from ``theodosia.adapter``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from burr.core import Action, Application
from fastmcp import Context, FastMCP

from theodosia.adapter import (
    ActionExecutionError,
    ActionTimeoutError,
    ApplicationFactory,
    InvalidTransitionError,
    ValidationFailed,
    _action_signature_params,
    _current_session_entry,
    _record_history,
    _session_app_and_lock,
    _SessionStore,
    _step_application,
    valid_next_action_names,
)


def _run_action_bare(
    app: Application,
    action_obj: Action,
    inputs: dict[str, Any],
    timeout_seconds: float | None = None,
    validator: Callable | None = None,
) -> dict[str, Any]:
    """Run an action against current state without graph enforcement.

    Used by ``ServingMode.TOOLS`` when the requested action is not in
    the standard valid-next set. Bypasses Burr's transition machinery
    while still updating ``app.state``. Burr's tracker is not invoked
    on this path; tools mode opts into that trade-off.
    """
    raise NotImplementedError(
        "TOOLS mode was carved out of theodosia.adapter; this helper is preserved "
        "for reference. To revive, restore the original body from git history."
    )


def _make_tool_handler(
    shared_app: Application,
    shared_lock: asyncio.Lock,
    factory: ApplicationFactory | None,
    store: _SessionStore,
    action: Action,
    enforce_transitions: bool,
    refresh_session_visibility: bool = False,
    timeout_seconds: float | None = None,
    validator: Callable | None = None,
) -> Callable:
    """Build an async MCP tool handler for one Burr action.

    The handler's signature is dynamically constructed to match the
    action's declared inputs so FastMCP introspects the correct
    JSON Schema. Body resolves the session-scoped Application,
    delegates to ``_step_application``, and records the attempt in
    the session's history.
    """
    params = _action_signature_params(action)
    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Context,
        default=None,
    )
    action_name = action.name

    async def handler(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs.pop("ctx", None)
        app, lock, entry = _session_app_and_lock(ctx, shared_app, shared_lock, factory, store)
        token = _current_session_entry.set(entry)
        subruns_before = set(entry.subruns) if entry is not None else set()
        try:
            async with lock:
                out = await _step_application(
                    app,
                    action_name=action_name,
                    inputs=kwargs,
                    enforce_transitions=enforce_transitions,
                    timeout_seconds=timeout_seconds,
                    validator=validator,
                    ctx=ctx,
                )
        except ValidationFailed as e:
            _record_history(
                store,
                ctx,
                factory,
                action=action_name,
                inputs=kwargs,
                state_after=None,
                valid_next_actions=valid_next_action_names(app),
                refused=True,
                refusal_reason="validation_failed",
                error_message=e.reason,
                error_type="ValidationFailed",
            )
            return {
                "error": "validation_failed",
                "requested": action_name,
                "reason": e.reason,
                "details": e.details,
                "valid_next_actions": valid_next_action_names(app),
            }
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
        finally:
            _current_session_entry.reset(token)
        new_subruns: list[str] = []
        if entry is not None:
            new_subruns = [s for s in entry.subruns if s not in subruns_before]
        _record_history(
            store,
            ctx,
            factory,
            action=action_name,
            inputs=kwargs,
            state_after=out["state"],
            valid_next_actions=out["valid_next_actions"],
            subruns=new_subruns or None,
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


class _DynamicRefusalMiddleware:
    """Convert FastMCP's 'tool not enabled' error into a structured refusal.

    In ``ServingMode.DYNAMIC``, per-session visibility filtering hides
    tools that are not in the current valid-next set, and FastMCP raises
    ``NotFoundError`` when a stale client (or one without proper
    ``tools/list_changed`` support) calls one anyway. STEP mode returns
    the rich ``invalid_transition`` shape; this middleware brings DYNAMIC
    to parity so the agent can recover from one error rather than
    seeing an opaque ``Unknown tool`` message.
    """

    def __init__(
        self,
        *,
        shared_app: Application,
        shared_lock: asyncio.Lock,
        factory: ApplicationFactory | None,
        store: _SessionStore,
        action_names: set[str],
    ) -> None:
        from fastmcp.server.middleware import Middleware as _Mw

        self._base_cls = _Mw
        self._shared_app = shared_app
        self._shared_lock = shared_lock
        self._factory = factory
        self._store = store
        self._action_names = action_names

    async def on_call_tool(self, context, call_next):
        from fastmcp.exceptions import NotFoundError
        from fastmcp.tools.base import ToolResult

        try:
            return await call_next(context)
        except NotFoundError:
            tool_name = getattr(context.message, "name", None)
            if tool_name not in self._action_names:
                raise
            ctx = context.fastmcp_context
            app, _, _ = _session_app_and_lock(
                ctx, self._shared_app, self._shared_lock, self._factory, self._store
            )
            valid = valid_next_action_names(app)
            payload = {
                "error": "invalid_transition",
                "requested": tool_name,
                "valid_next_actions": valid,
                "message": (
                    f"action {tool_name!r} is not reachable from current state. "
                    f"Valid actions now: {valid}."
                ),
            }
            return ToolResult(structured_content=payload)


def _make_dynamic_refusal_middleware(
    shared_app: Application,
    shared_lock: asyncio.Lock,
    factory: ApplicationFactory | None,
    store: _SessionStore,
) -> Any:
    from fastmcp.server.middleware import Middleware

    action_names = {a.name for a in shared_app.graph.actions}
    inner = _DynamicRefusalMiddleware(
        shared_app=shared_app,
        shared_lock=shared_lock,
        factory=factory,
        store=store,
        action_names=action_names,
    )

    class _Mw(Middleware):
        async def on_call_tool(self, context, call_next):
            return await inner.on_call_tool(context, call_next)

    return _Mw()


def _refresh_global_dynamic_visibility(mcp: FastMCP, app: Application) -> None:
    """Set the server-wide baseline visibility for DYNAMIC mode."""
    valid = set(valid_next_action_names(app))
    all_action_names = {a.name for a in app.graph.actions}
    enable_tags = {f"action:{n}" for n in valid}
    disable_tags = {f"action:{n}" for n in all_action_names - valid}
    if disable_tags:
        mcp.disable(tags=disable_tags)
    if enable_tags:
        mcp.enable(tags=enable_tags)


async def _refresh_session_dynamic_visibility(ctx: Context, app: Application) -> None:
    """Per-session visibility refresh for DYNAMIC mode."""
    valid = set(valid_next_action_names(app))
    all_action_names = {a.name for a in app.graph.actions}
    enable_tags = {f"action:{n}" for n in valid}
    disable_tags = {f"action:{n}" for n in all_action_names - valid}
    if disable_tags:
        await ctx.disable_components(tags=disable_tags)
    if enable_tags:
        await ctx.enable_components(tags=enable_tags)
