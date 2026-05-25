"""SPIKE: a hash-chained, tamper-evident audit ledger for Theodosia.

Each recorded event (a step or a refusal) is appended as one JSONL line that
carries ``prev`` (the previous line's hash) and ``hash`` (sha256 over the
previous hash plus this entry's canonical encoding). Editing, reordering, or
deleting any earlier line breaks every later hash, so ``verify_ledger`` can
point at the exact line where the chain diverges.

This is the honest version of the "tamper-evident audit log" the brand mockup
claimed but the shipped product does not yet have. It is a standalone prototype:
the real integration point is the adapter's recording path (where
``_record_history`` / ``_append_refusal_sidecar`` write today), which would call
``HashChainedLedger.append`` alongside the in-memory history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS = "sha256:" + "0" * 64


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev: str, entry_without_hash: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256((prev + _canonical(entry_without_hash)).encode()).hexdigest()


class HashChainedLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line).get("hash", last)
        return last

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        prev = self._last_hash()
        entry = {**event, "prev": prev}
        entry["hash"] = _digest(prev, entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")
        return entry


def verify_ledger(path: str | Path) -> tuple[bool, list[str]]:
    """Recompute the chain. Returns (ok, problems). Each problem names the line
    where the recorded hash or the prev-link does not match a recomputation."""
    p = Path(path)
    if not p.exists():
        return True, []
    problems: list[str] = []
    prev = GENESIS
    with p.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            stored = entry.get("hash")
            recomputed = _digest(prev, {k: v for k, v in entry.items() if k != "hash"})
            if entry.get("prev") != prev:
                problems.append(f"line {i}: prev-link mismatch (chain broken before here)")
            if stored != recomputed:
                problems.append(f"line {i}: hash mismatch (entry was altered)")
            prev = stored
    return (not problems), problems
