"""OpenTelemetry span emission for actions, via Burr's bridge.

theodosia doesn't write its own OTel adapter; Burr already has
``OpenTelemetryBridge``. The contribution here is that the bridge
works transparently through the MCP wire when wired into the
Application factory via ``.with_hooks(...)``.

These tests use OTel's in-memory exporter to capture spans without
any external collector. The export pipeline runs in the same process
as the test, so spans land in the in-memory list within the test
body.
"""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.integrations.opentelemetry import OpenTelemetryBridge
from fastmcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from theodosia import ServingMode, mount

_MODULE_EXPORTER = InMemorySpanExporter()


def _install_module_provider_once() -> None:
    """OTel only allows the global tracer provider to be set once per
    process. We install a single provider at module-import time, hold
    onto a single in-memory exporter, and have each test clear it
    before/after. Subsequent ``set_tracer_provider`` calls would warn
    and be ignored, so we avoid them entirely.
    """
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_MODULE_EXPORTER))
        trace.set_tracer_provider(provider)


_install_module_provider_once()


@pytest.fixture
def otel_exporter():
    """Clear the module-level exporter before and after each test so
    each test sees only its own spans."""
    _MODULE_EXPORTER.clear()
    yield _MODULE_EXPORTER
    _MODULE_EXPORTER.clear()


@action(reads=[], writes=["a"])
async def first(state: State) -> State:
    return state.update(a=1)


@action(reads=["a"], writes=["b"])
async def second(state: State) -> State:
    return state.update(b=state.get("a", 0) + 1)


def _factory_with_otel():
    return (
        ApplicationBuilder()
        .with_actions(first=first, second=second)
        .with_transitions(("first", "second"))
        .with_hooks(OpenTelemetryBridge(tracer_name="theodosia.test"))
        .with_state(a=None, b=None)
        .with_entrypoint("first")
        .build()
    )


def _factory_without_otel():
    return (
        ApplicationBuilder()
        .with_actions(first=first, second=second)
        .with_transitions(("first", "second"))
        .with_state(a=None, b=None)
        .with_entrypoint("first")
        .build()
    )


@pytest.mark.asyncio
async def test_each_action_run_emits_a_span(otel_exporter):
    server = mount(_factory_with_otel, mode=ServingMode.STEP, name="otel-test")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "first", "inputs": {}})
        await client.call_tool("step", {"action": "second", "inputs": {}})

    spans = otel_exporter.get_finished_spans()
    # Burr's bridge emits one span per action run. Two action runs
    # => at least two spans (it may also emit framing spans depending
    # on Burr's internal use of the bridge).
    action_span_names = {s.name for s in spans}
    assert "first" in action_span_names
    assert "second" in action_span_names


@pytest.mark.asyncio
async def test_no_spans_without_bridge(otel_exporter):
    """An Application without the OpenTelemetryBridge hook emits no
    theodosia-attributable spans. The in-memory exporter stays empty
    (modulo any framing spans the runtime itself emits)."""
    server = mount(_factory_without_otel, mode=ServingMode.STEP, name="no-otel")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "first", "inputs": {}})

    spans = otel_exporter.get_finished_spans()
    action_names = {s.name for s in spans}
    assert "first" not in action_names
    assert "second" not in action_names


@pytest.mark.asyncio
async def test_span_has_status_ok_on_successful_run(otel_exporter):
    """A successful action run produces a span with OK status. Burr's
    bridge populates attributes on spans, but the exact keys may
    evolve, so we assert the more stable signal (status) rather than
    a specific attribute set."""
    server = mount(_factory_with_otel, mode=ServingMode.STEP, name="otel-status")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "first", "inputs": {}})

    spans = otel_exporter.get_finished_spans()
    first_span = next((s for s in spans if s.name == "first"), None)
    assert first_span is not None
    # OK status is the default for a span that ended without recording
    # an error. We just verify the span ended cleanly.
    assert first_span.status.status_code.value in (0, 1)  # UNSET or OK


@pytest.mark.asyncio
async def test_streaming_action_still_emits_span(otel_exporter):
    """Streaming actions also flow through the bridge."""
    from burr.core.action import streaming_action

    @streaming_action(reads=[], writes=["story"])
    async def narrate(state: State):
        yield {"chunk": "hello"}, None
        yield {"chunk": ""}, state.update(story="hello")

    def factory():
        return (
            ApplicationBuilder()
            .with_actions(narrate=narrate)
            .with_hooks(OpenTelemetryBridge(tracer_name="theodosia.streaming"))
            .with_state(story=None)
            .with_entrypoint("narrate")
            .build()
        )

    server = mount(factory, mode=ServingMode.STEP, name="otel-streaming")
    async with Client(server) as client:
        r = await client.call_tool("step", {"action": "narrate", "inputs": {}})
        out = r.structured_content
        assert out["streamed"] is True

    spans = otel_exporter.get_finished_spans()
    assert "narrate" in {s.name for s in spans}
