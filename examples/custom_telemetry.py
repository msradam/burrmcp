"""User-defined span + attribute telemetry via Burr lifecycle hooks.

Burr exposes a ``__tracer`` magic parameter for actions that want to
emit sub-spans inside an action body. Each ``__tracer("span_name")``
call returns a context manager; entering it fires
``PreStartSpanHook.pre_start_span``, exiting fires
``PostEndSpanHook.post_end_span``, and ``span.log_attributes(...)``
fires ``DoLogAttributeHook.do_log_attributes``.

This demo captures those three hooks into a per-action span tree.
The companion ``with_otel`` demo uses Burr's prebuilt
``OpenTelemetryBridge`` to forward the same events to a real OTel
collector; this one shows the raw hook surface so you can build any
custom span sink (in-memory store, Datadog, Honeycomb, a database)
without the OTel dependency.

Domain: an artificial "render report" action that opens three
sub-spans (fetch, render, summarize), logs an attribute inside each,
and captures the tree via the custom hook. Exposed at
``burr://spans``.

Run:

    uv run python examples/custom_telemetry.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.lifecycle.base import (
    DoLogAttributeHook,
    PostEndSpanHook,
    PreStartSpanHook,
)
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "custom-telemetry-demo"


# == span sink =======================================================


class SpanCollector(PreStartSpanHook, PostEndSpanHook, DoLogAttributeHook):
    """Captures every span open/close and every attribute log into a
    per-action history. The structure preserved is:

        spans[action_name] -> list of {span, attributes, dependencies}

    where each span entry's ``attributes`` is a flat dict accumulated
    from any ``span.log_attributes(...)`` calls made before the span
    ended.
    """

    def __init__(self) -> None:
        self._open: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.spans: dict[str, list[dict[str, Any]]] = defaultdict(list)

    @staticmethod
    def _action_name(action: Any) -> str:
        return getattr(action, "name", str(action))

    def pre_start_span(
        self,
        *,
        action: Any,
        action_sequence_id: int,
        span: Any,
        span_dependencies: list[str],
        **_: Any,
    ) -> None:
        name = self._action_name(action)
        key = (name, action_sequence_id, span.uid)
        self._open[key] = {
            "name": span.name,
            "uid": span.uid,
            "attributes": {},
            "dependencies": list(span_dependencies),
        }

    def do_log_attributes(
        self,
        *,
        attributes: dict[str, Any],
        action: Any,
        action_sequence_id: int,
        span: Any | None,
        **_: Any,
    ) -> None:
        if span is None:
            return
        name = self._action_name(action)
        key = (name, action_sequence_id, span.uid)
        entry = self._open.get(key)
        if entry is not None:
            entry["attributes"].update(attributes)

    def post_end_span(
        self,
        *,
        action: Any,
        action_sequence_id: int,
        span: Any,
        **_: Any,
    ) -> None:
        name = self._action_name(action)
        key = (name, action_sequence_id, span.uid)
        entry = self._open.pop(key, None)
        if entry is not None:
            self.spans[name].append(entry)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {action_name: list(entries) for action_name, entries in self.spans.items()}


# == actions =========================================================


@action(reads=[], writes=["report"])
def render_report(state: State, __tracer, title: str = "Untitled") -> State:
    """Render a tiny "report" inside three sub-spans.

    Each sub-span logs at least one attribute so the
    DoLogAttributeHook fires.
    """
    with __tracer("fetch") as span:
        span.log_attributes(source="memory", item_count=3)
        items = [f"item-{i}" for i in range(3)]
    with __tracer("render") as span:
        span.log_attributes(title=title, items=len(items))
        body = "\n".join(f"- {item}" for item in items)
    with __tracer("summarize") as span:
        summary = f"{title}: {len(items)} items"
        span.log_attributes(summary=summary)
    return state.update(report={"title": title, "body": body, "summary": summary})


# == graph ===========================================================


def build_application(*, sink: SpanCollector | None = None):
    builder = (
        ApplicationBuilder()
        .with_actions(render_report=render_report)
        .with_state(report=None)
        .with_entrypoint("render_report")
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
    )
    if sink is not None:
        builder = builder.with_hooks(sink)
    return builder.build()


def build_server():
    sink = SpanCollector()

    def factory():
        return build_application(sink=sink)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="custom-telemetry",
        instructions=(
            "Single action 'render_report(title: str)' that opens three "
            "sub-spans (fetch, render, summarize) and logs attributes "
            "inside each. Custom SpanCollector hook captures every "
            "span and attribute log; read burr://spans for the tree."
        ),
    )

    @server.resource("burr://spans")
    async def _spans_resource() -> str:
        return json.dumps(sink.snapshot(), indent=2, default=str)

    return server


if __name__ == "__main__":
    build_server().run()
