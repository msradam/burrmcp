"""Hash-chained audit ledger.

Every recorded attempt (a step or a refusal) is appended as one JSONL line
carrying ``prev`` (the previous line's hash) and ``hash`` (sha256 or
HMAC-sha256 over the previous hash plus this entry's canonical encoding).
Editing, reordering, or inserting any earlier line breaks every later hash,
so ``verify_ledger`` points at the exact line where the chain diverges.

What the chain proves and does not prove:

* **Proves**: in-place edits, reorderings, duplications, and middle-deletions
  of recorded entries (with the exact offending line called out).
* **Does not prove on its own**:
  - *Truncation* (dropping the tail-most entry leaves a chain that still
    self-verifies). Detect by external commitment of expected-length, or by
    streaming each entry to append-only storage as it is written.
  - *Whole-cloth forgery* under the default (unkeyed) mode. The hash
    function is public, so a holder of write access to ``ledger.jsonl`` can
    mint a chain from scratch. Set ``THEODOSIA_LEDGER_KEY`` (hex-encoded
    bytes) in the server's environment to switch the chain to HMAC; forgery
    then requires the key.
  - *Cross-session replay* unless the entries carry their ``app_id`` and
    ``project`` in the hashed payload. ``mount()`` binds both by default, so
    copying a ``ledger.jsonl`` from session A into session B's directory
    fails ``verify`` because the binding does not match the on-disk path.
  - *Origin*: a signature over the head would add non-repudiation; the chain
    alone does not.
  - *Existence of any particular session*: deleting a whole session
    directory is invisible to ``verify``; detect by external manifest.

For regulated audit trails, layer external commitments (RFC 3161 timestamp
authority, transparency log, append-only object storage with retention
locks) on top.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

GENESIS = "sha256:" + "0" * 64
GENESIS_HMAC = "hmac-sha256:" + "0" * 64


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def _resolve_key(explicit: bytes | None) -> bytes | None:
    """Return the HMAC key from ``explicit`` or the ``THEODOSIA_LEDGER_KEY`` env
    var (hex-encoded). ``None`` means plain SHA256."""
    if explicit is not None:
        return explicit
    env = os.environ.get("THEODOSIA_LEDGER_KEY")
    if not env:
        return None
    try:
        return bytes.fromhex(env)
    except ValueError:
        return env.encode()


def _digest(prev: str, entry_without_hash: dict[str, Any], key: bytes | None) -> str:
    payload = (prev + _canonical(entry_without_hash)).encode()
    if key is None:
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


class HashChainedLedger:
    """Append-only JSONL chain. One instance per ``ledger.jsonl`` file.

    ``binding`` is an optional dict of session identity fields (``app_id``,
    ``project``, ``partition_key``) that lands inside every entry's hashed
    payload. The adapter binds these by default so a ledger cannot be moved
    to a different session directory and still verify.

    ``key`` is an optional HMAC key. ``None`` (the default) uses plain
    SHA256; pass bytes (or set ``THEODOSIA_LEDGER_KEY`` in the env as a hex
    string) to switch to HMAC-SHA256, which makes forgery require the key.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        binding: dict[str, Any] | None = None,
        key: bytes | None = None,
    ):
        self.path = Path(path)
        self.binding = dict(binding or {})
        self.key = _resolve_key(key)

    def _genesis(self) -> str:
        return GENESIS_HMAC if self.key is not None else GENESIS

    def _last_hash(self) -> str:
        if not self.path.exists():
            return self._genesis()
        last = self._genesis()
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line).get("hash", last)
        return last

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Chain ``event`` onto the ledger and return the written entry
        (the event plus its ``prev``, ``binding``, and ``hash``)."""
        prev = self._last_hash()
        entry = event | {"prev": prev}
        if self.binding:
            entry["binding"] = self.binding
        entry["hash"] = _digest(prev, entry, self.key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")
        return entry


def verify_ledger(
    path: str | Path,
    *,
    expected_binding: dict[str, Any] | None = None,
    key: bytes | None = None,
    expected_min_entries: int | None = None,
) -> tuple[bool, list[str]]:
    """Recompute the chain. Returns ``(ok, problems)``.

    Each problem names the line where the recorded hash, the prev-link, the
    binding, or an entry-count expectation fails. A missing file is vacuously
    valid.

    ``expected_binding`` (typically ``{"app_id": ..., "project": ...}``)
    refuses entries whose stored binding does not match; this is what makes
    cross-session replay (copying ledger.jsonl to a different app dir)
    detectable.

    ``key`` (or ``THEODOSIA_LEDGER_KEY`` env) verifies an HMAC-keyed chain;
    pass ``None`` for the default unkeyed chain.

    ``expected_min_entries`` refuses a ledger shorter than the given count;
    use it with an external claim of recorded length to detect truncation.
    """
    p = Path(path)
    if not p.exists():
        return True, []
    resolved_key = _resolve_key(key)
    genesis = GENESIS_HMAC if resolved_key is not None else GENESIS
    problems: list[str] = []
    prev = genesis
    count = 0
    with p.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            stored = entry.get("hash")
            payload = {k: v for k, v in entry.items() if k != "hash"}
            recomputed = _digest(prev, payload, resolved_key)
            if entry.get("prev") != prev:
                problems.append(f"line {i}: prev-link mismatch (chain broken before here)")
            if stored != recomputed:
                problems.append(f"line {i}: hash mismatch (entry was altered)")
            if expected_binding is not None:
                got = entry.get("binding") or {}
                missing = [k for k, v in expected_binding.items() if got.get(k) != v]
                if missing:
                    problems.append(
                        f"line {i}: binding mismatch on {missing} "
                        f"(ledger does not belong to this session)"
                    )
            prev = stored
            count += 1
    if expected_min_entries is not None and count < expected_min_entries:
        problems.append(
            f"truncation: ledger has {count} entries; expected at least {expected_min_entries}"
        )
    return (not problems), problems


def ledger_count(path: str | Path) -> int:
    """Return the number of non-empty lines in ``path``. 0 if missing."""
    p = Path(path)
    if not p.exists():
        return 0
    with p.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())
