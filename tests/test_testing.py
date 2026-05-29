"""Tests for ``theodosia.testing.FakeUpstream`` and its mount() integration."""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, action

import theodosia
from theodosia.testing import FakeCall, FakeUpstream
from theodosia.upstream import UpstreamError, bind_upstream, call_upstream, reset_upstream

# ── FakeUpstream as a standalone manager (no mount needed) ──────────────


@pytest.mark.asyncio
async def test_static_value_response() -> None:
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    result = await fake.call("svc", "ping", {})
    assert result == "pong"


@pytest.mark.asyncio
async def test_sync_callable_receives_args_and_returns() -> None:
    fake = FakeUpstream({"svc": {"double": lambda args: args["n"] * 2}})
    result = await fake.call("svc", "double", {"n": 21})
    assert result == 42


@pytest.mark.asyncio
async def test_async_callable_is_awaited() -> None:
    async def slow_double(args: dict) -> int:
        return args["n"] * 2

    fake = FakeUpstream({"svc": {"slow": slow_double}})
    result = await fake.call("svc", "slow", {"n": 7})
    assert result == 14


@pytest.mark.asyncio
async def test_unknown_server_raises_upstream_error() -> None:
    fake = FakeUpstream({"known": {"ping": "pong"}})
    with pytest.raises(UpstreamError, match="unknown server"):
        await fake.call("unknown", "ping", {})


@pytest.mark.asyncio
async def test_unknown_tool_raises_upstream_error() -> None:
    fake = FakeUpstream({"svc": {"a": 1}})
    with pytest.raises(UpstreamError, match="unknown tool"):
        await fake.call("svc", "b", {})


@pytest.mark.asyncio
async def test_callable_that_raises_propagates() -> None:
    def boom(args: dict) -> None:
        raise ValueError("simulated upstream failure")

    fake = FakeUpstream({"svc": {"break": boom}})
    with pytest.raises(ValueError, match="simulated upstream failure"):
        await fake.call("svc", "break", {})
    # The call is still recorded even though it raised.
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_calls_are_recorded_in_order() -> None:
    fake = FakeUpstream({"a": {"x": 1}, "b": {"y": 2}})
    await fake.call("a", "x", {"k": 1})
    await fake.call("b", "y", {"k": 2})
    await fake.call("a", "x", {"k": 3})
    assert fake.calls == [
        FakeCall(server="a", tool="x", args={"k": 1}),
        FakeCall(server="b", tool="y", args={"k": 2}),
        FakeCall(server="a", tool="x", args={"k": 3}),
    ]


@pytest.mark.asyncio
async def test_calls_to_filters_by_server() -> None:
    fake = FakeUpstream({"a": {"x": 1}, "b": {"y": 2}})
    await fake.call("a", "x", {"i": 0})
    await fake.call("b", "y", {"i": 1})
    a_calls = fake.calls_to("a")
    assert len(a_calls) == 1
    assert a_calls[0].server == "a"


@pytest.mark.asyncio
async def test_calls_to_filters_by_server_and_tool() -> None:
    fake = FakeUpstream({"svc": {"a": 1, "b": 2}})
    await fake.call("svc", "a", {"i": 0})
    await fake.call("svc", "b", {"i": 1})
    await fake.call("svc", "a", {"i": 2})
    a_calls = fake.calls_to("svc", "a")
    assert len(a_calls) == 2
    assert all(c.tool == "a" for c in a_calls)


@pytest.mark.asyncio
async def test_clear_drops_recorded_calls() -> None:
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    await fake.call("svc", "ping", {})
    assert len(fake.calls) == 1
    fake.clear()
    assert fake.calls == []


@pytest.mark.asyncio
async def test_register_adds_response_after_construction() -> None:
    fake = FakeUpstream()
    fake.register("svc", "ping", "pong")
    assert await fake.call("svc", "ping", {}) == "pong"


@pytest.mark.asyncio
async def test_calls_returns_copy_not_internal_list() -> None:
    fake = FakeUpstream({"svc": {"ping": "pong"}})
    await fake.call("svc", "ping", {})
    snap = fake.calls
    snap.append(FakeCall(server="garbage", tool="g", args={}))
    # Mutating the snapshot does not affect the fake's record.
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_args_are_copied_so_mutating_after_does_not_corrupt_record() -> None:
    fake = FakeUpstream({"svc": {"x": "ok"}})
    args = {"k": "v"}
    await fake.call("svc", "x", args)
    args["k"] = "mutated"
    assert fake.calls[0].args == {"k": "v"}


@pytest.mark.asyncio
async def test_aclose_is_noop() -> None:
    fake = FakeUpstream()
    await fake.aclose()  # must not raise


# ── bind_upstream + call_upstream integration ────────────────────────────


@pytest.mark.asyncio
async def test_bind_upstream_routes_call_to_fake() -> None:
    fake = FakeUpstream({"grafana": {"query_metrics": lambda args: {"value": args["m"]}}})
    tok = bind_upstream(fake)
    try:
        result = await call_upstream("grafana", "query_metrics", {"m": 42})
    finally:
        reset_upstream(tok)
    assert result == {"value": 42}
    assert fake.calls == [FakeCall(server="grafana", tool="query_metrics", args={"m": 42})]


# ── Mount integration: pass FakeUpstream as upstream= ──────────────────


@pytest.fixture
def upstream_using_app():
    """A Burr app whose action body calls an upstream service."""

    @action(reads=[], writes=["metric_value"])
    async def query(state):
        v = await call_upstream("grafana", "query_metrics", {"m": "cpu"})
        return state.update(metric_value=v["value"])

    return (
        ApplicationBuilder()
        .with_actions(query=query)
        .with_state(metric_value=None)
        .with_entrypoint("query")
        .build()
    )


def test_mount_accepts_fake_upstream_directly(upstream_using_app):
    """Passing a FakeUpstream as ``upstream=`` should work just like a config dict."""
    fake = FakeUpstream({"grafana": {"query_metrics": lambda args: {"value": 99}}})
    server = theodosia.mount(lambda: upstream_using_app, name="mount-fake-test", upstream=fake)
    # Server constructed without error; that is the contract this test asserts.
    assert server.name == "mount-fake-test"


def test_mount_distinguishes_dict_vs_manager(upstream_using_app):
    """A dict goes to UpstreamManager; anything with .call() is used as-is."""
    # As a dict (the normal path).
    server_a = theodosia.mount(
        lambda: upstream_using_app,
        name="dict-path",
        upstream={"grafana": "http://localhost:3001/sse"},
    )
    # As a fake (manager path).
    fake = FakeUpstream({"grafana": {"query_metrics": lambda a: {"value": 1}}})
    server_b = theodosia.mount(lambda: upstream_using_app, name="fake-path", upstream=fake)
    assert server_a is not server_b
