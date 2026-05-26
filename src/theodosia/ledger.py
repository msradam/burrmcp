"""Hash-chained, tamper-evident audit ledger.

Every recorded attempt (a step or a refusal) is appended as one JSONL line
carrying ``prev`` (the previous line's hash) and ``hash`` (sha256 over the
previous hash plus this entry's canonical encoding). Editing, reordering, or
deleting any earlier line breaks every later hash, so ``verify_ledger`` points
at the exact line where the chain diverges.

The chain proves integrity (the record was not altered after the fact), not
confidentiality (it is not encrypted) and not origin (a signature over the head
would add non-repudiation; the chain alone does not). The adapter writes one
``ledger.jsonl`` next to each session's tracker log; ``theodosia verify`` checks
it.
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
    """Append-only JSONL chain. One instance per ``ledger.jsonl`` file."""

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
        """Chain ``event`` onto the ledger and return the written entry
        (the event plus its ``prev`` and ``hash``)."""
        prev = self._last_hash()
        entry = {**event, "prev": prev}
        entry["hash"] = _digest(prev, entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")
        return entry


def verify_ledger(path: str | Path) -> tuple[bool, list[str]]:
    """Recompute the chain. Returns ``(ok, problems)``; each problem names the
    line where the recorded hash or the prev-link does not match a
    recomputation. A missing file is vacuously valid."""
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
