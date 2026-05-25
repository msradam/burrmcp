"""upstream: theodosia as an MCP client to other servers.

A Burr action calls call_upstream(server, tool, args) from its body; the
call is forwarded to a real MCP server theodosia connected to as a client.
The agent only ever sees theodosia's `step` tool -- the upstream server is
not exposed to it (single surface). Every upstream call happens inside an
action, so it advances state (ledger).
"""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client, FastMCP

from theodosia import ServingMode, UpstreamManager, call_upstream, mount
from theodosia.upstream import UpstreamError, bind_upstream, reset_upstream

# == a fake upstream MCP server (in-process) =========================


def _make_upstream_server() -> FastMCP:
    up = FastMCP("fake-cluster")

    @up.tool
    def get_pods(namespace: str = "default") -> dict:
        return {"namespace": namespace, "pods": ["api-aa", "api-bb"], "crashing": ["api-aa"]}

    @up.tool
    def get_logs(pod: str) -> dict:
        return {"pod": pod, "logs": "FATAL: redis connection refused"}

    return up


# == UpstreamManager unit ============================================


@pytest.mark.asyncio
async def test_manager_calls_upstream_tool():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    out = await mgr.call("cluster", "get_pods", {"namespace": "prod"})
    assert out["namespace"] == "prod"
    assert "api-aa" in out["crashing"]
    await mgr.aclose()


@pytest.mark.asyncio
async def test_manager_unknown_server_raises():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    with pytest.raises(UpstreamError, match="unknown upstream server"):
        await mgr.call("nope", "get_pods", {})
    await mgr.aclose()


@pytest.mark.asyncio
async def test_call_upstream_requires_bound_manager():
    with pytest.raises(UpstreamError, match="No upstream manager bound"):
        await call_upstream("cluster", "get_pods", {})


@pytest.mark.asyncio
async def test_call_upstream_uses_bound_manager():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    token = bind_upstream(mgr)
    try:
        out = await call_upstream("cluster", "get_logs", {"pod": "api-aa"})
        assert "redis" in out["logs"]
    finally:
        reset_upstream(token)
        await mgr.aclose()


# == end-to-end: action calls upstream, driven via step =============


@action(reads=[], writes=["pods", "done"])
async def survey(state: State) -> State:
    """Survey the cluster by calling the upstream MCP server."""
    result = await call_upstream("cluster", "get_pods", {"namespace": "prod"})
    return state.update(pods=result.get("pods", []), done=False)


@action(reads=["pods"], writes=["report", "done"])
async def report(state: State) -> State:
    """Write a report from what was surveyed."""
    return state.update(report=f"{len(state['pods'])} pods seen", done=True)


def _build_app():
    _open = Condition.expr("done != True")
    return (
        ApplicationBuilder()
        .with_actions(survey=survey, report=report)
        .with_transitions(("survey", "report", _open))
        .with_state(pods=[], report=None, done=False)
        .with_entrypoint("survey")
        .build()
    )


@pytest.mark.asyncio
async def test_action_calls_upstream_through_mounted_server():
    """The agent calls only `step`; the survey action reaches the upstream
    cluster server through theodosia; the result lands in state."""
    server = mount(
        _build_app,
        mode=ServingMode.STEP,
        name="upstream-demo",
        upstream={"cluster": _make_upstream_server()},
    )
    async with Client(server) as client:
        # The agent's tool surface is just the theodosia meta-tools -- the
        # upstream cluster's get_pods/get_logs are NOT exposed here.
        tools = {t.name for t in await client.list_tools()}
        assert "step" in tools
        assert "get_pods" not in tools  # single surface: upstream hidden

        out = (
            await client.call_tool("step", {"action": "survey", "inputs": {}})
        ).structured_content
        assert "error" not in out
        # The upstream call ran inside the action; its result is in state.
        assert out["state"]["pods"] == ["api-aa", "api-bb"]
