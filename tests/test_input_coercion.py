"""Tests for the JSON-string-to-object input coercion middleware.

Some MCP clients (e.g. IBM Bob as of mid-2026) serialize nested
object arguments as JSON strings on the wire instead of as nested
JSON objects. FastMCP's input-schema validator rejects those as
``params/X must be object``. burrmcp's mount() installs a middleware
that re-parses such strings before validation when the tool's
declared schema is object- or array-typed.

These tests exercise the schema-fragment helper directly plus a
live round-trip through the in-process Client where the
``arguments`` payload is rewritten on the way to the server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.server.middleware import MiddlewareContext

from burrmcp.adapter import _build_coercion_middleware, _expects_object_or_array

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "examples"))

from coffee_order import build_server

# ── schema helper ───────────────────────────────────────────────────


def test_expects_object_or_array_direct_type():
    assert _expects_object_or_array({"type": "object"}) is True
    assert _expects_object_or_array({"type": "array"}) is True
    assert _expects_object_or_array({"type": "string"}) is False
    assert _expects_object_or_array({"type": "integer"}) is False


def test_expects_object_or_array_list_of_types():
    assert _expects_object_or_array({"type": ["object", "null"]}) is True
    assert _expects_object_or_array({"type": ["array", "null"]}) is True
    assert _expects_object_or_array({"type": ["string", "null"]}) is False


def test_expects_object_or_array_anyof():
    # step's actual inputs schema shape.
    schema = {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]}
    assert _expects_object_or_array(schema) is True


def test_expects_object_or_array_oneof_with_string():
    # A union of string and object — still object-allowed.
    schema = {"oneOf": [{"type": "string"}, {"type": "object"}]}
    assert _expects_object_or_array(schema) is True


def test_expects_object_or_array_empty_or_invalid():
    assert _expects_object_or_array({}) is False
    assert _expects_object_or_array(None) is False  # type: ignore[arg-type]


# ── live coercion through the in-process Client ─────────────────────


@pytest.mark.asyncio
async def test_real_step_schema_drives_inputs_coercion():
    """Against the live step tool's schema, an ``inputs`` string gets
    coerced to a dict because the schema declares the param as
    ``anyOf: [object, null]``."""
    server = build_server()
    middleware = _build_coercion_middleware()
    # Prime the lookup off the real server (the schema for step ships
    # an object-or-null inputs param).
    middleware._schemas = await middleware._build_lookup(server)
    assert "step" in middleware._schemas
    assert _expects_object_or_array(middleware._schemas["step"].get("inputs", {}))

    msg = mt.CallToolRequestParams(
        name="step",
        arguments={"action": "take_order", "inputs": '{"item": "mocha"}'},
    )
    seen: list[mt.CallToolRequestParams] = []

    async def call_next(context):
        seen.append(context.message)
        return

    ctx = MiddlewareContext(message=msg, source="client", method="tools/call")
    await middleware.on_call_tool(ctx, call_next)

    # The forwarded message has `inputs` parsed as a dict.
    assert seen[0].arguments == {
        "action": "take_order",
        "inputs": {"item": "mocha"},
    }


@pytest.mark.asyncio
async def test_normal_object_inputs_still_work():
    """The middleware is a no-op on the well-formed object path."""
    server = build_server()
    async with Client(server) as client:
        r = await client.call_tool(
            "step",
            {"action": "take_order", "inputs": {"item": "latte"}},
        )
        payload = json.loads(r.content[0].text)
        assert payload.get("error") is None, payload
        assert payload["state"]["item"] == "latte"


@pytest.mark.asyncio
async def test_string_inputs_round_trip_through_server():
    """End-to-end: a client that sends ``inputs`` as a JSON string still
    reaches the action body with a parsed dict.

    This is the Bob-shaped failure mode (mid-2026): nested object
    arguments serialized as JSON strings on the wire. With
    strict_input_validation off plus the coercion middleware, the
    request succeeds.
    """
    server = build_server()
    async with Client(server) as client:
        # Client.call_tool serializes whatever it's given; passing a
        # string for `inputs` matches what Bob does over the wire.
        r = await client.call_tool(
            "step",
            {"action": "take_order", "inputs": '{"item": "mocha"}'},
        )
        payload = json.loads(r.content[0].text)
        assert payload.get("error") is None, payload
        assert payload["state"]["item"] == "mocha"
        assert payload["state"]["stage"] == "ordered"


@pytest.mark.asyncio
async def test_non_json_string_left_alone():
    """A string that's not valid JSON for an object-typed param is
    passed through; FastMCP's validator will reject it cleanly."""
    middleware = _build_coercion_middleware()

    # Build a context with arguments containing a non-JSON string.
    fake_msg = mt.CallToolRequestParams(
        name="step", arguments={"action": "take_order", "inputs": "not-json-at-all"}
    )
    seen: list[mt.CallToolRequestParams] = []

    async def call_next(context):
        seen.append(context.message)
        return

    ctx = MiddlewareContext(message=fake_msg, source="client", method="tools/call")
    # No fastmcp_context, so the lookup short-circuits and the message
    # passes through untouched. This matches the "library missed it" path.
    await middleware.on_call_tool(ctx, call_next)
    assert seen[0].arguments == {"action": "take_order", "inputs": "not-json-at-all"}


@pytest.mark.asyncio
async def test_string_in_actual_string_field_left_alone():
    """If the schema says ``type: string`` we don't touch the value."""
    middleware = _build_coercion_middleware()
    middleware._schemas = {"demo": {"text": {"type": "string"}, "blob": {"type": "object"}}}
    msg = mt.CallToolRequestParams(
        name="demo",
        arguments={
            "text": '{"foo": "bar"}',  # looks like JSON but the schema says string
            "blob": '{"x": 1}',  # the schema says object, this should be parsed
        },
    )
    seen: list[mt.CallToolRequestParams] = []

    async def call_next(context):
        seen.append(context.message)
        return

    ctx = MiddlewareContext(message=msg, source="client", method="tools/call")
    await middleware.on_call_tool(ctx, call_next)
    out = seen[0].arguments
    assert out["text"] == '{"foo": "bar"}', "string-typed field must stay a string"
    assert out["blob"] == {"x": 1}, "object-typed field must be coerced"
