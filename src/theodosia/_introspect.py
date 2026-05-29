"""Pure-function introspection helpers used by ``mount()`` and the resource handlers.

State sanitization, action input/schema inspection, and the per-action
timeout / validator resolvers. These functions do not touch the session
store or the FSM driver loop; they only inspect Burr objects and types.

Re-exported from :mod:`theodosia.adapter` for backwards compatibility.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import typing
from typing import Any

import pydantic
from burr.core import Application
from burr.core.action import Action, Condition

from theodosia._exceptions import ValidationFailed

_INTERNAL_STATE_KEYS = frozenset({"__SEQUENCE_ID", "__PRIOR_STEP"})

# Function attribute set by ``theodosia.importing`` (and any other code
# that wants to annotate an action with a per-call timeout). ``mount``
# reads this off each action's ``fn`` and uses it in preference to the
# server-wide default.
_PER_ACTION_TIMEOUT_ATTR = "_theodosia_timeout_seconds"


def _public_state(state_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state_dict.items() if k not in _INTERNAL_STATE_KEYS}


def _serializable_state(state_dict: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a JSON-serialisable copy of ``state_dict`` plus a list of
    keys whose values had to be stringified.

    Burr lets actions put anything in state (numpy arrays, connection
    objects, Pydantic models, callables). The wire format is JSON, so we
    test each top-level value with strict ``json.dumps`` and fall back to
    ``str(value)`` for anything that fails. The list of coerced keys is
    surfaced to the client via ``_theodosia.coerced_keys`` on the state
    resource so it knows the round-trip is lossy and can flag downstream.

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


def _action_timeout(action: Action, server_default: float | None) -> float | None:
    """Return the timeout to use for ``action``.

    Per-action override (set by ``ToolSpec.timeout_seconds`` via the
    importer, or by hand-tagging a function with
    ``fn._theodosia_timeout_seconds = N``) wins over the server-wide
    default. ``None`` at either level disables timeout for that level; a
    numeric override on the action applies even when the server default
    is ``None``.
    """
    fn = getattr(action, "fn", None)
    per_action = getattr(fn, _PER_ACTION_TIMEOUT_ATTR, None) if fn is not None else None
    if per_action is not None:
        return float(per_action)
    return server_default


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
            # Condition that depends on state keys not yet set is treated
            # as not-reachable, same as Burr's own behavior.
            continue
    return valid


def _action_inputs(action: Action) -> tuple[list[str], list[str]]:
    """Return ``(required, optional)`` input names for an action."""
    raw = action.inputs
    if isinstance(raw, tuple) and len(raw) == 2:
        req, opt = raw
        return req.copy(), opt.copy()
    return list(raw or []), []


def _pydantic_model_in_annotation(ann: Any) -> type | None:
    """Return the Pydantic model class in ``ann``, unwrapping ``Optional`` / ``Union``.

    Returns the model class for ``OrderInput``, ``Optional[OrderInput]``,
    ``Union[OrderInput, None]``, and ``OrderInput | None``. Returns ``None``
    for any annotation that doesn't carry exactly one Pydantic model.
    """
    if ann is None:
        return None
    if isinstance(ann, type) and issubclass(ann, pydantic.BaseModel):
        return ann
    origin = typing.get_origin(ann)
    if origin is None:
        return None
    args = [a for a in typing.get_args(ann) if a is not type(None)]
    if len(args) != 1:
        return None
    inner = args[0]
    if isinstance(inner, type) and issubclass(inner, pydantic.BaseModel):
        return inner
    return None


def _coerce_pydantic_inputs(action: Action, inputs: dict[str, Any]) -> dict[str, Any]:
    """Coerce dict input values into their declared Pydantic model type.

    A typed action ``foo(state, order: OrderInput)`` receives ``order`` as a
    plain dict over MCP. Without coercion the action body would have to call
    ``OrderInput(**order)`` itself. With it, the body sees the typed object
    its signature annotated. Non-Pydantic inputs and non-dict values are
    passed through unchanged so this is safe for primitive-typed inputs and
    for clients that already construct the model.

    When a dict cannot validate as the declared model, raises
    ``ValidationFailed`` so the wire shape carries a clean
    ``validation_failed`` refusal with per-field details, rather than letting
    the action body crash with an ``AttributeError`` that surfaces as a
    generic ``action_error``.
    """
    fn = getattr(action, "fn", None)
    if fn is None or not inputs:
        return inputs
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        return inputs
    out = inputs.copy()
    for name, value in list(out.items()):
        ann = _pydantic_model_in_annotation(hints.get(name))
        if ann is None:
            continue
        if isinstance(value, ann):
            continue
        if not isinstance(value, dict):
            continue
        try:
            out[name] = ann(**value)
        except pydantic.ValidationError as exc:
            raise ValidationFailed(
                f"input {name!r} does not match {ann.__name__}: {exc.error_count()} error(s)",
                details={
                    "param": name,
                    "model": ann.__name__,
                    "errors": exc.errors(include_url=False, include_input=False),
                },
            ) from exc
        except TypeError as exc:
            raise ValidationFailed(
                f"input {name!r} cannot be constructed as {ann.__name__}: {exc}",
                details={"param": name, "model": ann.__name__, "errors": [str(exc)]},
            ) from exc
    return out


def _annotation_to_schema(ann: Any) -> dict[str, Any]:
    if ann is None:
        return {"type": "any"}
    if isinstance(ann, type) and issubclass(ann, pydantic.BaseModel):
        with contextlib.suppress(Exception):
            return ann.model_json_schema()
    type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if ann in type_map:
        return {"type": type_map[ann]}
    if ann in (list, tuple):
        return {"type": "array"}
    if ann is dict:
        return {"type": "object"}
    origin = typing.get_origin(ann)
    if origin in (list, tuple):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    if origin is typing.Literal:
        return {"enum": list(typing.get_args(ann))}
    return {"type": getattr(ann, "__name__", str(ann))}


def _input_schemas(action: Action) -> dict[str, dict[str, Any]]:
    """Return a JSON-schema-ish description of each input parameter.

    For a Pydantic-typed input, surfaces ``model_json_schema()`` so an
    agent can see the nested object shape. For built-in types, surfaces a
    short ``{"type": "...", "default": ...}``. Anything we can't read
    falls back to ``{"type": "any"}``.

    Without this, ``theodosia://graph`` would say only
    ``required_inputs: ["order"]`` and the agent would have no idea
    ``order`` is ``{"item": str, "qty": int}``.
    """
    fn = getattr(action, "fn", None)
    if fn is None:
        return {}
    try:
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
    except Exception:
        return {}
    required, optional = _action_inputs(action)
    schemas: dict[str, dict[str, Any]] = {}
    for name in (*required, *optional):
        ann = hints.get(name)
        if ann is None and name in sig.parameters:
            p = sig.parameters[name]
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else None
        schemas[name] = _annotation_to_schema(ann)
        if name in sig.parameters:
            p = sig.parameters[name]
            if p.default is not inspect.Parameter.empty:
                with contextlib.suppress(TypeError, ValueError):
                    json.dumps(p.default)
                    schemas[name]["default"] = p.default
    return schemas


def _action_signature_params(action: Action) -> list[inspect.Parameter]:
    """Synthesise a Parameter list for an action's MCP tool signature.

    Burr action functions take ``state`` plus the declared inputs. The MCP
    tool sees only the inputs; ``state`` is supplied internally. Default
    values and type annotations come from the wrapped function where
    available; otherwise inputs default to ``str``.

    Resolves ``from __future__ import annotations`` postponed evaluation
    via ``typing.get_type_hints`` so the annotations are real types rather
    than strings, which FastMCP's pydantic-based schema generation requires.
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
