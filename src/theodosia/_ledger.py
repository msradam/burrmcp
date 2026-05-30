"""Hash-chained ledger writes + refusal sidecar.

Two append-only audit surfaces live next to Burr's per-session
``log.jsonl``:

* ``ledger.jsonl`` - every attempt (success or refusal), hash-chained.
  Read back by ``theodosia verify`` to detect after-the-fact edits,
  reorders, and middle-deletions.
* ``refusals.jsonl`` - refusals only, plain JSONL. Burr's tracker only
  records actions that ran, so an ``invalid_transition`` (the graph
  blocking an out-of-order call) never reaches the on-disk log without
  this sidecar.

Both are no-ops when the Application has no ``LocalTrackingClient``,
so a graph mounted without a tracker keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from burr.core import Application

from theodosia._tracker import _tracker_log_path
from theodosia.ledger import HashChainedLedger

_LOG = logging.getLogger("theodosia")
_KEY_WARNING_EMITTED = False
_KEY_WARNING_LOCK = threading.Lock()


def _warn_unkeyed_once() -> None:
    """Warn once per process when the ledger is running in unkeyed mode.

    SHA-only mode still detects edits, reorders, and middle-deletions, but
    cannot defend against an operator with write access who mints a chain
    from scratch. The warning fires the first time a ledger row is written
    without ``THEODOSIA_LEDGER_KEY`` set, so a deploy that flips the env in
    the middle of a process still triggers the warning at the next write.
    """
    global _KEY_WARNING_EMITTED
    if _KEY_WARNING_EMITTED:
        return
    if os.environ.get("THEODOSIA_LEDGER_KEY"):
        # Keyed mode at write time. Do NOT consume the once-warning budget
        # so a subsequent env-var unset still emits the first warning.
        return
    with _KEY_WARNING_LOCK:
        if _KEY_WARNING_EMITTED:
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


# Process-local cache of the last hash per ledger path. ``HashChainedLedger``
# computes ``_last_hash`` by reading the entire file on construction, so a
# naive "construct + append" pattern is O(n) per append (O(n^2) over a session).
# We cache the path -> last_hash mapping, populated by HashChainedLedger on
# first read, and updated by ``append``. Concurrent same-path writers in one
# process serialize on ``_LEDGER_CACHE_LOCK``. Cross-process concurrency is
# not handled; shared-app mode across processes against the same ledger path
# is not supported.
_LEDGER_LAST_HASH: dict[str, str | None] = {}
_LEDGER_CACHE_LOCK = threading.Lock()


def _append_ledger(app: Application, record: dict[str, Any]) -> None:
    """Chain one attempt (step or refusal) onto the session's hash-chained
    ledger, next to the tracker log.

    Unlike ``refusals.jsonl`` (refusals only, for ``theodosia logs --refusals``),
    the ledger covers every attempt and is hash-chained, so ``theodosia verify``
    can detect any after-the-fact edit, reorder, or middle-deletion. No-op when
    the Application has no local tracker. Append failures are logged at WARNING
    rather than raised, so a step that ran on the wire is never blocked by an
    audit-log write failure; operators monitoring the ``theodosia`` logger see
    the failure in real time.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    _warn_unkeyed_once()
    ledger_path = log_path.parent / "ledger.jsonl"
    key = str(ledger_path)
    try:
        with _LEDGER_CACHE_LOCK:
            ledger = HashChainedLedger(
                ledger_path,
                binding=_ledger_binding(app, log_path),
                last_hash=_LEDGER_LAST_HASH.get(key),
            )
            entry = ledger.append(record)
            _LEDGER_LAST_HASH[key] = entry.get("hash")
    except OSError as exc:
        _LOG.warning("theodosia ledger append failed at %s: %s", ledger_path, exc)


def _append_refusal_sidecar(app: Application, record: dict[str, Any]) -> None:
    """Persist a refusal next to the Burr tracker log, so the durable audit
    trail includes blocked transitions, not just executed steps.

    Burr's ``LocalTrackingClient`` only logs actions that ran, so an
    ``invalid_transition`` (the graph blocking an out-of-order call) never
    reaches the on-disk log. We append it to a ``refusals.jsonl`` sidecar in
    the same app directory; ``theodosia logs --refusals`` reads both. No-op
    when the Application has no local tracker; append failures log at WARNING.
    """
    log_path = _tracker_log_path(app)
    if log_path is None:
        return
    sidecar = log_path.parent / "refusals.jsonl"
    try:
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("theodosia refusal-sidecar append failed at %s: %s", sidecar, exc)
