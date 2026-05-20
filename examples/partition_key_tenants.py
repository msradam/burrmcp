"""Per-tenant Applications via ``with_identifiers(partition_key=...)``.

Burr's ``ApplicationBuilder.with_identifiers(app_id=, partition_key=,
sequence_id=)`` sets identifiers that flow through to persistence and
tracking. ``partition_key`` is the load-bearing one for multi-tenant
deployments: a persister that supports partitioning (most of them
do, including ``LocalTrackingClient``) uses it to isolate data
between groups of applications. Two tenants writing to the same
backing store with different ``partition_key`` values can't see each
other's runs.

This demo:

* Builds a tiny customer-record FSM (``open_record`` -> ``update_note``
  -> ``close_record``).
* Uses ``with_identifiers(partition_key=<tenant_id>)`` in the factory.
* ``tenant_id`` defaults to ``"default"``; override via the
  ``BURRMCP_TENANT_ID`` env var when launching the server so the
  same code serves multiple tenants from separate processes.
* Adds a custom ``burr://tenant`` resource that exposes the current
  partition_key so an MCP client can see which tenant's data this
  session is operating in.

The Burr-level claim this validates: the partition_key set via
``with_identifiers`` is visible on the live ``Application`` (as
``app._partition_key``) and flows into the tracker's per-app
records. Reading two different partition_keys' tracker JSONL on
disk (or calling ``fork_from_past`` against a persister that
queries by partition_key) returns different data.

Run (default tenant):

    uv run python examples/partition_key_tenants.py

Run (specific tenant):

    BURRMCP_TENANT_ID=acme uv run python examples/partition_key_tenants.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "partition-key-tenants-demo"
_DEFAULT_TENANT = "default"


# == actions =========================================================


@action(reads=[], writes=["customer_id", "stage", "notes"])
def open_record(state: State, customer_id: str) -> State:
    """Open a customer record."""
    if not customer_id.strip():
        raise ValueError("customer_id must not be empty")
    return state.update(customer_id=customer_id, stage="open", notes=[])


@action(reads=["notes", "stage"], writes=["notes"])
def update_note(state: State, note: str) -> State:
    """Append a note to the customer record."""
    if state["stage"] != "open":
        raise ValueError(f"cannot update notes in stage {state['stage']!r}")
    stamped = f"[{datetime.now(timezone.utc).isoformat()}] {note}"
    return state.update(notes=[*state["notes"], stamped])


@action(reads=["stage"], writes=["stage", "closed_at"])
def close_record(state: State) -> State:
    """Close the record. Terminal."""
    return state.update(stage="closed", closed_at=datetime.now(timezone.utc).isoformat())


# == graph ===========================================================


def _tenant_id() -> str:
    return os.environ.get("BURRMCP_TENANT_ID", _DEFAULT_TENANT)


def build_application():
    """Build an Application with the partition_key set to the current
    tenant. All persistence and tracking writes for this Application
    will land under the partition_key, isolated from other tenants.
    """
    tenant = _tenant_id()
    is_open = Condition.expr("stage == 'open'")
    return (
        ApplicationBuilder()
        .with_identifiers(partition_key=tenant)
        .with_actions(
            open_record=open_record,
            update_note=update_note,
            close_record=close_record,
        )
        .with_transitions(
            ("open_record", "update_note", is_open),
            ("open_record", "close_record", is_open),
            ("update_note", "update_note", is_open),
            ("update_note", "close_record", is_open),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(customer_id=None, stage="new", notes=[], closed_at=None)
        .with_entrypoint("open_record")
        .build()
    )


def build_server():
    server = mount(
        build_application,
        mode=ServingMode.STEP,
        name="partition-key-tenants",
        instructions=(
            "Per-tenant customer-record FSM. Each session's "
            "Application is built with "
            "with_identifiers(partition_key=<tenant_id>); the tenant "
            "comes from the BURRMCP_TENANT_ID env var (default: "
            f"{_DEFAULT_TENANT!r}). Walk: open_record(customer_id) -> "
            "update_note(note) [loop] -> close_record. Read "
            "burr://tenant to see the current partition_key; "
            "burr://session for the full tracker coordinates."
        ),
    )

    @server.resource("burr://tenant")
    async def _tenant_resource() -> str:
        """The tenant identifier baked into this server's factory."""
        return json.dumps(
            {
                "tenant_id": _tenant_id(),
                "source": "BURRMCP_TENANT_ID env var (or default)",
            },
            indent=2,
        )

    return server


if __name__ == "__main__":
    build_server().run()
