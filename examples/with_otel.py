"""OpenTelemetry spans for every action, surfaced through Burr's
lifecycle hooks.

Burr ships an ``OpenTelemetryBridge`` adapter that emits a span per
action run. Wire it into the Application factory via ``.with_hooks(...)``
and the spans are everywhere ``ActionExecution`` happens: through
``step``, through ``spawn_subapp``, through ``astream_result``,
through ``fork_at``. Each span carries the action name, sequence id,
state diff, and any inputs as attributes.

This example shows the standard wire-up with a stdout exporter so
running ``uv run python examples/with_otel.py``, then making a few
tool calls against it, prints spans to stderr. Swap the exporter for
OTLP, Jaeger, Honeycomb, etc. by changing one line.

Install: ``pip install 'theodosia[observability]'``.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.integrations.opentelemetry import OpenTelemetryBridge
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from theodosia import ServingMode, mount

# ── OTel wiring (do once at process start) ──────────────────────────

trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))


# ── Burr graph ──────────────────────────────────────────────────────


@action(reads=[], writes=["greeted"])
async def greet(state: State, name: str) -> State:
    return state.update(greeted=name)


@action(reads=["greeted"], writes=["farewell"])
async def farewell(state: State) -> State:
    return state.update(farewell=f"Goodbye, {state.get('greeted', 'friend')}")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(greet=greet, farewell=farewell)
        .with_transitions(("greet", "farewell"))
        .with_hooks(OpenTelemetryBridge(tracer_name="theodosia.example"))
        .with_state(greeted=None, farewell=None)
        .with_entrypoint("greet")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="with-otel",
        instructions=(
            "Two-step greeting FSM with OpenTelemetry spans emitted "
            "around every action via Burr's OpenTelemetryBridge. "
            "Spans land in stdout via ConsoleSpanExporter in this "
            "example; in production replace with OTLP/Jaeger/etc."
        ),
    )


if __name__ == "__main__":
    build_server().run()
