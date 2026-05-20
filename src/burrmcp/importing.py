"""Lift an existing FastMCP server into a Burr Application.

The shape of ``burrmcp`` so far has been one-way: write a Burr graph,
mount it as an MCP server. The other direction is the more common
starting point: someone already has a FastMCP server with a flat list
of tools, and they want to gain transition enforcement, audit history,
and per-session isolation without rewriting everything.

This module takes that flat server plus a declaration of the implicit
state machine and produces a ``burr.core.Application`` ready to pass
back to ``burrmcp.mount``. The implicit state machine isn't inferred,
it's declared, because guessing reads/writes from parameter names is
lossy and silently wrong. The user names which tools mutate which
state keys and which transitions are valid. The library handles the
signature wiring and the re-exposure.

Typical pattern:

    flat = FastMCP("legacy")

    @flat.tool
    async def create_order(item: str) -> dict:
        return {"order_id": "ORD-1", "item": item}

    @flat.tool
    async def pay(order_id: str, amount: float) -> dict:
        return {"paid": True}

    @flat.tool
    async def fulfill(order_id: str) -> dict:
        return {"status": "fulfilled"}

    app = await burr_app_from_fastmcp(
        flat,
        initial_state={"order_id": None, "paid": False},
        tool_specs={
            "create_order": ToolSpec(writes=["order_id"], merge_result=True),
            "pay":          ToolSpec(reads=["order_id"], writes=["paid"], merge_result=True),
            "fulfill":      ToolSpec(reads=["order_id", "paid"]),
        },
        transitions=[("create_order", "pay"), ("pay", "fulfill")],
        entrypoint="create_order",
    )

    server = mount(app, mode=ServingMode.STEP, name="lifted")

What carries over from the original tools:

  • Parameter names, types, and defaults (preserved on the wrapped
    function's signature so burrmcp's later schema generation sees
    the same inputs the user already wrote).
  • Docstrings (used as the action's description).
  • Async/sync nature (wrapper preserves it).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import FastMCP


@dataclass
class ToolSpec:
    """Declares how one FastMCP tool participates in the Burr graph.

    ``reads`` / ``writes`` mirror Burr's ``@action`` parameters: which
    keys of shared state the action consults and which it can mutate.
    Tools that don't touch shared state get empty lists and act purely
    as transition gates (they run side effects, the graph moves on).

    ``merge_result`` controls how the tool's return value flows into
    state. When True, the tool's return is expected to be a dict; keys
    matching ``writes`` are merged into state (other keys are ignored).
    When False, the tool's return value is discarded and state is left
    unchanged. ``state_update`` is an optional explicit callable that
    takes the tool's result and returns a ``{key: value}`` dict to
    apply; it overrides ``merge_result``.

    ``rename`` lets the user change the action's name in the resulting
    Burr graph without renaming the original tool. Useful when two
    flat servers each had a ``status`` tool and they're being merged.

    ``timeout_seconds`` declares a per-action timeout. The importer
    annotates the wrapped action with this value via a function
    attribute; ``mount`` reads it back and applies it only to this
    action, taking precedence over the server-wide
    ``action_timeout_seconds`` on ``mount``. Set to ``None`` (the
    default) to inherit the server-wide setting.

    ``validator`` declares an input validator that runs before the
    tool fires. It receives ``(state_dict, inputs)`` and may raise
    ``burrmcp.ValidationFailed`` to refuse, return a dict to
    substitute normalised inputs, or return None to accept the
    originals unchanged. Same shape as the ``input_validators={}``
    mapping on ``mount``; per-tool here takes precedence over the
    server-wide map.
    """

    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    merge_result: bool = False
    state_update: Callable[[Any], dict[str, Any]] | None = None
    rename: str | None = None
    timeout_seconds: float | None = None
    validator: Callable[[dict, dict], dict | None] | None = None


def _build_wrapper(
    tool_fn: Callable,
    tool_name: str,
    tool_doc: str | None,
    spec: ToolSpec,
):
    """Wrap a FastMCP tool function as a Burr-compatible action.

    The wrapper has the original tool's parameters plus a leading
    ``state`` parameter so Burr's ``@action`` machinery sees it as a
    standard action. State mutation rules come from ``spec``: nothing
    by default, ``writes``-matched keys from the return dict if
    ``merge_result`` is True, or whatever ``state_update`` returns if
    provided.

    The wrapper's ``__signature__`` and ``__annotations__`` are set
    explicitly because burrmcp's later schema generation reintrospects
    them when mounting the resulting Application.
    """
    original_sig = inspect.signature(tool_fn)
    original_params = list(original_sig.parameters.values())
    new_params = [
        inspect.Parameter("state", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        *original_params,
    ]
    new_sig = inspect.Signature(parameters=new_params)
    is_async = asyncio.iscoroutinefunction(tool_fn)
    writes_set = set(spec.writes)

    def _apply_result(state: State, result: Any) -> State:
        if spec.state_update is not None:
            update = spec.state_update(result) or {}
            return state.update(**update) if update else state
        if spec.merge_result and isinstance(result, dict):
            filtered = {k: v for k, v in result.items() if k in writes_set}
            return state.update(**filtered) if filtered else state
        return state

    if is_async:

        async def wrapper(state: State, **kwargs: Any) -> State:
            result = await tool_fn(**kwargs)
            return _apply_result(state, result)

    else:

        def wrapper(state: State, **kwargs: Any) -> State:
            result = tool_fn(**kwargs)
            return _apply_result(state, result)

    wrapper.__name__ = spec.rename or tool_name
    wrapper.__qualname__ = wrapper.__name__
    wrapper.__doc__ = tool_doc or f"Lifted FastMCP tool {tool_name!r}."
    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    # Copy annotations from the original function so type hints survive
    # for downstream schema generation. ``state`` gets no annotation.
    wrapper.__annotations__ = dict(getattr(tool_fn, "__annotations__", {}))
    wrapper.__annotations__.pop("return", None)
    # Stash per-tool overrides on the wrapper as discoverable
    # attributes. ``mount`` reads them off each action's ``fn`` to
    # pick up these per-action settings.
    if spec.timeout_seconds is not None:
        wrapper._burrmcp_timeout_seconds = spec.timeout_seconds  # type: ignore[attr-defined]
    if spec.validator is not None:
        wrapper._burrmcp_validator = spec.validator  # type: ignore[attr-defined]
    return wrapper


def _normalize_transitions(
    transitions: list[tuple[str, str] | tuple[str, str, Condition | str]] | None,
    rename_map: dict[str, str],
) -> list[tuple[str, str, Condition] | tuple[str, str]]:
    """Apply ``ToolSpec.rename`` to transitions and lift str conditions.

    A transition entry may be ``(from, to)`` or ``(from, to, cond)``
    where ``cond`` is either a ``burr.core.action.Condition`` or a
    Python expression string. Strings are lifted via
    ``Condition.expr(...)`` for convenience so users don't have to
    import ``Condition`` themselves.
    """
    out: list = []
    for t in transitions or []:
        if len(t) == 2:
            src, dst = t
            out.append((rename_map.get(src, src), rename_map.get(dst, dst)))
        elif len(t) == 3:
            src, dst, cond = t
            if isinstance(cond, str):
                cond = Condition.expr(cond)
            out.append((rename_map.get(src, src), rename_map.get(dst, dst), cond))
        else:
            raise ValueError(f"transition must be (from, to) or (from, to, cond), got {t!r}")
    return out


async def burr_app_from_fastmcp(
    server: FastMCP,
    *,
    entrypoint: str,
    initial_state: dict[str, Any] | None = None,
    tool_specs: dict[str, ToolSpec] | None = None,
    transitions: list | None = None,
    only: list[str] | None = None,
) -> Application:
    """Lift a FastMCP server into a Burr Application.

    Args:
        server: An existing FastMCP server with tools registered.
        entrypoint: Name of the action (tool name, or its ``rename``)
            that's the graph's starting node.
        initial_state: Starting state values. Omitted keys default to
            ``None`` when first read.
        tool_specs: Per-tool declaration of reads/writes/merge_result.
            Tools not listed here are wrapped as stateless actions
            (no reads, no writes, return discarded).
        transitions: List of ``(from, to)`` or ``(from, to, cond)``
            tuples. ``cond`` may be a ``Condition`` or a Python
            expression string. Names refer to action names after
            applying ``ToolSpec.rename``.
        only: Optional allowlist of tool names to lift. Tools not in
            this list are skipped. Use this when a server has tools
            that don't belong in the state machine (helpers, health
            checks, etc.).

    Returns:
        A built ``burr.core.Application`` ready to pass to
        ``burrmcp.mount``.
    """
    tool_specs = tool_specs or {}
    initial_state = initial_state or {}

    tools = await server.list_tools()
    rename_map: dict[str, str] = {}
    burr_actions: dict[str, Any] = {}

    for tool in tools:
        if only is not None and tool.name not in only:
            continue
        spec = tool_specs.get(tool.name, ToolSpec())
        if spec.rename:
            rename_map[tool.name] = spec.rename
        wrapped = _build_wrapper(
            tool_fn=tool.fn,
            tool_name=tool.name,
            tool_doc=tool.description,
            spec=spec,
        )
        burr_action = action(reads=spec.reads, writes=spec.writes)(wrapped)
        burr_actions[spec.rename or tool.name] = burr_action

    if entrypoint not in burr_actions:
        raise ValueError(
            f"entrypoint {entrypoint!r} is not among the lifted actions: {sorted(burr_actions)}"
        )

    normalized_transitions = _normalize_transitions(transitions, rename_map)

    builder = (
        ApplicationBuilder()
        .with_actions(**burr_actions)
        .with_state(**initial_state)
        .with_entrypoint(entrypoint)
    )
    if normalized_transitions:
        builder = builder.with_transitions(*normalized_transitions)
    return builder.build()
