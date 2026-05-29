"""Input-coercion middleware: JSON-string args → objects/arrays.

Some MCP clients (notably IBM Bob as of mid-2026) serialise nested
object arguments as JSON strings instead of as nested objects. FastMCP's
input-schema validator then rejects them. This middleware re-parses
those strings when the tool's declared schema accepts object or array.

Lives in its own module so the FastMCP middleware base class import
stays lazy and ``adapter.py`` stays focused on ``mount()``.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP


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

    Lazily imports the FastMCP Middleware base so this module doesn't pay
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
