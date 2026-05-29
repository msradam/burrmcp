"""``fork_from_past`` binds ``partition_key`` to the calling session's identity.

Regression for a security finding from a persona dogfood (SRE / SOC 2
review): without this binding, a caller can pass any ``partition_key``
to ``fork_from_past`` and load another tenant's persisted state, since
the persister load only cares whether ``(partition_key, app_id)`` exists.

The session's factory writes its partition via
``with_identifiers(partition_key=...)``. We pin to that value when the
caller passes the empty default, and refuse with ``partition_mismatch``
when the caller passes a different non-empty value.
"""

from __future__ import annotations

from typing import Any

import pytest
from burr.core import ApplicationBuilder, State, action
from fastmcp import Client

from theodosia import mount


@action(reads=[], writes=["x"])
def go(state: State, n: int = 1) -> State:
    return state.update(x=state.get("x", 0) + n)


class _ExposingLoader:
    """Stand-in BaseStateLoader that records every load() call and would
    happily serve any (partition_key, app_id) it was asked about. Lets us
    prove the binding refuses BEFORE the loader is reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def load(
        self,
        *,
        partition_key: str,
        app_id: str,
        sequence_id: int | None = None,
    ) -> dict[str, Any] | None:
        self.calls.append((partition_key, app_id))
        return None  # nothing actually persisted; the refusal path is the focus


def _factory_for(tenant: str):
    def _build():
        return (
            ApplicationBuilder()
            .with_identifiers(partition_key=tenant, app_id=f"{tenant}-app")
            .with_actions(go=go)
            .with_state(x=0)
            .with_entrypoint("go")
            .build()
        )

    return _build


@pytest.mark.asyncio
async def test_caller_supplied_partition_key_mismatch_is_refused():
    """Tenant A's session must not be able to load Tenant B's state by
    passing Tenant B's partition_key as a tool argument."""
    loader = _ExposingLoader()
    server = mount(_factory_for("acme"), state_loader=loader, name="t")
    async with Client(server) as c:
        r = await c.call_tool(
            "fork_from_past",
            {"app_id": "globex-app", "sequence_id": -1, "partition_key": "globex"},
        )
    out = r.structured_content
    assert out["error"] == "partition_mismatch"
    assert out["requested"] == "globex"
    # And: the loader was NEVER called. The refusal must fire before any
    # storage backend is reached.
    assert loader.calls == []


@pytest.mark.asyncio
async def test_empty_partition_key_uses_session_binding():
    """Caller passes the empty default; the bound partition_key from the
    session's Application is filled in instead. This is the existing
    single-tenant happy path and must keep working."""
    loader = _ExposingLoader()
    server = mount(_factory_for("acme"), state_loader=loader, name="t")
    async with Client(server) as c:
        await c.call_tool(
            "fork_from_past",
            {"app_id": "some-app", "sequence_id": -1, "partition_key": ""},
        )
    assert loader.calls == [("acme", "some-app")]


@pytest.mark.asyncio
async def test_matching_partition_key_passes_through():
    """If the caller passes the partition_key the session is already
    bound to, it should pass through cleanly (no spurious refusal)."""
    loader = _ExposingLoader()
    server = mount(_factory_for("acme"), state_loader=loader, name="t")
    async with Client(server) as c:
        await c.call_tool(
            "fork_from_past",
            {"app_id": "acme-app", "sequence_id": -1, "partition_key": "acme"},
        )
    assert loader.calls == [("acme", "acme-app")]
