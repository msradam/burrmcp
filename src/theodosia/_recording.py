"""Record-and-replay upstream-call trajectories.

``RecordingUpstream`` wraps an upstream manager and captures every call
to a JSONL fixture. ``ReplayingUpstream`` serves recorded results in
order; a call that doesn't match the next recorded entry raises
``ReplayMismatch``.

Fixture format is one ``{"server", "tool", "args", "result"}`` JSON
object per line. Hand-editable for chaos injection.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class ReplayMismatch(RuntimeError):
    """A replayed call did not match the next recorded call."""


class _Recorder:
    """Shared accumulator for recorded (server, tool, args, result) entries."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, server: str, tool: str, args: dict[str, Any], result: Any) -> None:
        self.entries.append(
            {
                "server": server,
                "tool": tool,
                "args": args.copy(),
                "result": result,
            }
        )


class RecordingUpstream:
    """Manager wrapper that records every upstream call; ``save`` writes JSONL."""

    def __init__(self, wrapped: Any) -> None:
        if not hasattr(wrapped, "call"):
            raise TypeError(
                f"RecordingUpstream requires an object with async call(); "
                f"got {type(wrapped).__name__}"
            )
        self._wrapped = wrapped
        self._rec = _Recorder()
        self._lock = asyncio.Lock()

    @property
    def entries(self) -> list[dict[str, Any]]:
        """All recorded entries in call order. Returns a copy."""
        return [e.copy() for e in self._rec.entries]

    @property
    def server_names(self) -> list[str]:
        return getattr(self._wrapped, "server_names", [])

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        result = await self._wrapped.call(server, tool, args)
        async with self._lock:
            self._rec.record(server, tool, args, result)
        return result

    async def aclose(self) -> None:
        aclose = getattr(self._wrapped, "aclose", None)
        if aclose is not None:
            await aclose()

    def save(self, path: str | Path) -> None:
        """Write the recorded trajectory to a JSONL file."""
        Path(path).expanduser().write_text(
            "\n".join(json.dumps(e, default=str) for e in self._rec.entries) + "\n",
            encoding="utf-8",
        )


class ReplayingUpstream:
    """Manager that serves recorded results in order; mismatches raise ``ReplayMismatch``.

    With ``strict_args=True`` (default) the args dict must match exactly. With
    ``strict_args=False`` only ``server`` and ``tool`` are compared.
    """

    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        strict_args: bool = True,
    ) -> None:
        self._entries = [e.copy() for e in entries]
        self._strict_args = strict_args
        self._cursor = 0
        self._lock = asyncio.Lock()

    @classmethod
    def from_file(cls, path: str | Path, *, strict_args: bool = True) -> ReplayingUpstream:
        """Load a JSONL trajectory fixture from disk."""
        text = Path(path).expanduser().read_text(encoding="utf-8")
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
        return cls(entries, strict_args=strict_args)

    @property
    def server_names(self) -> list[str]:
        return sorted({e["server"] for e in self._entries})

    @property
    def total(self) -> int:
        return len(self._entries)

    @property
    def consumed(self) -> int:
        return self._cursor

    @property
    def remaining(self) -> int:
        return len(self._entries) - self._cursor

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        async with self._lock:
            if self._cursor >= len(self._entries):
                raise ReplayMismatch(
                    f"call({server!r}, {tool!r}, ...) past end of trajectory; "
                    f"{len(self._entries)} entries already consumed"
                )
            entry = self._entries[self._cursor]
            self._cursor += 1
        if entry["server"] != server or entry["tool"] != tool:
            raise ReplayMismatch(
                f"expected call({entry['server']!r}, {entry['tool']!r}) but got "
                f"call({server!r}, {tool!r}) at entry {self._cursor - 1}"
            )
        if self._strict_args and entry["args"] != args:
            raise ReplayMismatch(
                f"call({server!r}, {tool!r}) args mismatch at entry "
                f"{self._cursor - 1}: expected {entry['args']!r}, got {args!r}"
            )
        return entry["result"]

    async def aclose(self) -> None:
        return None

    def reset(self) -> None:
        """Rewind to the start of the trajectory; useful between sub-runs."""
        self._cursor = 0
