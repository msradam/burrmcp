"""Tamper-evident ledger writes + refusal sidecar.

Two append-only audit surfaces live next to Burr's per-session
``log.jsonl``:

* ``ledger.jsonl`` - every attempt (success or refusal), hash-chained.
  Read back by ``theodosia verify`` to detect after-the-fact edits.
* ``refusals.jsonl`` - refusals only, plain JSONL. Burr's tracker only
  records actions that ran, so an ``invalid_transition`` (the graph
  blocking an out-of-order call) never reaches the on-disk log without
  this sidecar.

Both are no-ops when the Application has no ``LocalTrackingClient``,
so a graph mounted without a tracker keeps working.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from burr.core import Application

from theodosia._tracker import _tracker_log_path
from theodosia.ledger import HashChainedLedger

_LOG = logging.getLogger("theodosia")
_KEY_WARNING_EMITTED = False


def _warn_unkeyed_once() -> None:
    """Emit a single startup warning when the ledger is running in unkeyed mode.

    Logged once per process the first time a ledger row is written without
    ``THEODOSIA_LEDGER_KEY``. SHA-only mode still detects edits, reorders, and
    middle-deletions, but cannot defend against an operator with write access
    who mints a chain from scratch. The warning surfaces the gap so operators
    deploying to production set the HMAC key intentionally rather than by
    accident. ``theodosia doctor`` carries the same finding statically.
    """
    global _KEY_WARNING_EMITTED
    if _KEY_WARNING_EMITTED:
        return
    if os.environ.get("THEODOSIA_LEDGER_KEY"):
        _KEY_WARNING_EMITTED = True
        return
    _KEY_WARNING_EMITTED = True
    _LOG.warning(
        "Theodosia ledger is running in unkeyed SHA-256 mode; this detects "
        "edits, reorders, and middle-deletions but not whole-cloth forgery. "
        "Set THEODOSIA_LEDGER_KEY=<hex> for HMAC-keyed chains in production."
    )


def _ledger_binding(app: Application, log_path: Path) -> dict[str, Any]:
    """Identity fields hashed into every ledger entry.

    Embedding these in the chain means copying ``ledger.jsonl`` between
    session directories breaks verification: ``verify`` is called with the
    on-disk ``app_id`` / ``project`` and refuses entries whose binding does
    not match.
    """
    return {
        "app_id": log_path.parent.name,
        "project": log_path.parent.parent.name,
        "partition_key": getattr(app, "partition_key", None),
    }


def _append_ledger(app: Application, record: dict[str, Any]) -> None:
    """Chain one attempt (step or refusal) onto the session's tamper-evident
    ledger, next to the tracker log.

    Unlike ``refusals.jsonl`` (refusals only, for ``theodosia logs --refusals``),
    the ledger covers every attempt and is hash-chained, so ``theodosia verify``
    can detect any after-the-fact edit. No-op when the Application has no local
    tracker.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    _warn_unkeyed_once()
    with contextlib.suppress(OSError):
        ledger = HashChainedLedger(
            log_path.parent / "ledger.jsonl",
            binding=_ledger_binding(app, log_path),
        )
        ledger.append(record)


def _append_refusal_sidecar(app: Application, record: dict[str, Any]) -> None:
    """Persist a refusal next to the Burr tracker log, so the durable audit
    trail includes blocked transitions, not just executed steps.

    Burr's ``LocalTrackingClient`` only logs actions that ran, so an
    ``invalid_transition`` (the graph blocking an out-of-order call) never
    reaches the on-disk log. We append it to a ``refusals.jsonl`` sidecar in
    the same app directory; ``theodosia logs --refusals`` reads both. No-op
    when the Application has no local tracker.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    sidecar = log_path.parent / "refusals.jsonl"
    with contextlib.suppress(OSError), sidecar.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
