"""Burr's ``@trace`` decorator: auto-spanned sub-functions.

Companion to ``custom_telemetry``. That demo used ``__tracer(...)``
inside an action body to open named sub-spans explicitly. ``@trace``
flips the polarity: decorate any function with ``@trace()`` and
when it's called from inside an action body, it automatically opens
a span around its execution, logs its bound arguments as attributes,
logs its return value, and nests inside any outer trace context.
Outside an action, ``@trace`` is a no-op.

The nested call graph maps directly onto the span tree. A nested
call from one ``@trace`` function into another produces a nested
span. Same three lifecycle hooks fire as in ``custom_telemetry``
(PreStartSpanHook, PostEndSpanHook, DoLogAttributeHook).

Domain: a tiny text analyzer. The ``analyze(text)`` action calls
``tokenize(text)`` which calls ``count_words(tokens)``; both are
``@trace``-decorated. The resulting span tree shows the call
hierarchy automatically. Tree at ``theodosia://trace-spans``.

Run:

    uv run python examples/trace_decorator.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.lifecycle.base import DoLogAttributeHook, PostEndSpanHook, PreStartSpanHook
from burr.visibility import trace

from theodosia import ServingMode, mount

# == span sink =======================================================


class TraceSpanCollector(PreStartSpanHook, PostEndSpanHook, DoLogAttributeHook):
    """Captures every span (auto-opened by ``@trace`` or manually via
    ``__tracer``) with its attributes and the parent/child structure.
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
        **_: Any,
    ) -> None:
        name = self._action_name(action)
        self._open[(name, action_sequence_id, span.uid)] = {
            "name": span.name,
            "uid": span.uid,
            "parent_uid": getattr(getattr(span, "parent", None), "uid", None),
            "attributes": {},
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
        entry = self._open.get((name, action_sequence_id, span.uid))
        if entry is not None:
            entry["attributes"].update({k: str(v) for k, v in attributes.items()})

    def post_end_span(
        self,
        *,
        action: Any,
        action_sequence_id: int,
        span: Any,
        **_: Any,
    ) -> None:
        name = self._action_name(action)
        entry = self._open.pop((name, action_sequence_id, span.uid), None)
        if entry is not None:
            self.spans[name].append(entry)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {action_name: list(entries) for action_name, entries in self.spans.items()}


# == @trace-decorated helpers ========================================


@trace()
def tokenize(text: str) -> list[str]:
    """Split text into lowercase words. Auto-spanned by @trace."""
    return [t.lower() for t in text.split() if t]


@trace()
def count_words(tokens: list[str]) -> dict[str, int]:
    """Count word frequencies. Auto-spanned; nested inside tokenize's
    parent span when called from analyze."""
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


# == action ==========================================================


@action(reads=[], writes=["analysis"])
def analyze(state: State, text: str = "the quick brown fox") -> State:
    """Tokenize, count, return summary. Both helper calls open their
    own spans via @trace; @trace sees the surrounding action's tracer
    context and threads the spans through Burr's hook system.
    """
    tokens = tokenize(text)
    counts = count_words(tokens)
    return state.update(
        analysis={
            "tokens": tokens,
            "unique_count": len(counts),
            "total_count": sum(counts.values()),
        }
    )


# == graph ===========================================================


def build_application(*, sink: TraceSpanCollector | None = None):
    builder = (
        ApplicationBuilder()
        .with_actions(analyze=analyze)
        .with_state(analysis=None)
        .with_entrypoint("analyze")
    )
    if sink is not None:
        builder = builder.with_hooks(sink)
    return builder.build()


def build_server():
    sink = TraceSpanCollector()

    def factory():
        return build_application(sink=sink)

    server = mount(
        factory,
        mode=ServingMode.STEP,
        name="trace-decorator",
        instructions=(
            "Single action 'analyze(text: str)' that internally calls "
            "tokenize() and count_words(), both wrapped with @trace. "
            "Each helper call auto-opens a span with its inputs/outputs "
            "logged as attributes. Read theodosia://trace-spans for the "
            "captured tree."
        ),
    )

    @server.resource("theodosia://trace-spans")
    async def _spans_resource() -> str:
        return json.dumps(sink.snapshot(), indent=2, default=str)

    return server


if __name__ == "__main__":
    build_server().run()
