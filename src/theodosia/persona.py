"""Personas: mountable identity layer for Theodosia-served FSMs.

A persona is to an FSM what an actor is to a script: it doesn't change the
procedure (the Burr graph), it changes who's executing it. Same incident
investigation FSM driven by a careful-SRE persona produces a different
trajectory than the same FSM driven by a risk-taking persona, even though
the workflow is the same and the audit trail uses identical action names.

Format (mirrors SKILL.md from the Agent Skills Open Standard):

    ---
    name: on-call-sre
    description: Calm on-call SRE; root cause first, blast radius before fix.
    voice: terse, direct, no hype       # optional
    metadata:
      version: "1.0"
    ---

    # body markdown: the actual instructions/identity

A persona ships as a PERSONA.md file. Theodosia mounts a directory of them
and exposes each as an MCP prompt the client can pick at session-start,
plus resources for inspection and (with allow_runtime_swap) a set_persona
tool for mid-session changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Matches `{state.phase}`, `{action.name}`, `{graph.total_actions}`, etc.
# Restricted to dot-separated identifier paths so we don't collide with
# legitimate uses of curly braces in markdown (e.g. code samples).
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)*)\}")


def render_with_frame(text: str, frame: dict[str, Any] | None) -> str:
    """Interpolate ``{path.to.value}`` placeholders against a frame dict.

    Used to make persona bodies frame-aware: a persona that references
    ``{state.alert_id}`` or ``{action.name}`` gets rendered against the
    Burr Application's current state and position at prompt-fetch time.

    Unknown placeholders render as the empty string rather than raising,
    so a persona that references a state field the FSM hasn't populated
    yet (or whose name doesn't exist) renders cleanly with the missing
    pieces silently absent. Pass ``frame=None`` to skip interpolation
    entirely and return the text verbatim; this is the right behavior
    when the persona is being used at mount time before any session has
    a frame.
    """
    if frame is None:
        return text

    def _resolve(match: re.Match[str]) -> str:
        cur: Any = frame
        for part in match.group(1).split("."):
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
            if cur is None:
                return ""
        if cur is None:
            return ""
        # Render dicts and lists as JSON, not Python repr, so LLM consumers
        # see ``{"item": "soda"}`` rather than ``{'item': 'soda'}``.
        if isinstance(cur, dict | list | tuple):
            import json

            return json.dumps(cur, default=str)
        return str(cur)

    return _PLACEHOLDER.sub(_resolve, text)


@dataclass(frozen=True)
class Persona:
    """A parsed persona: frontmatter + body."""

    name: str
    description: str
    body: str
    voice: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, *, fallback_name: str | None = None) -> Persona:
        """Parse a PERSONA.md string into a Persona.

        The frontmatter is optional. If absent, the whole text becomes the
        body and ``fallback_name`` becomes the name (callers that load from
        a file path supply the filename stem here).
        """
        text = text.lstrip("﻿")  # strip BOM if present
        if text.startswith(("---\n", "---\r\n")):
            # Find the closing fence. Allow either \n--- or \r\n--- after.
            fm_start = text.index("\n") + 1
            close = text.find("\n---", fm_start)
            if close == -1:
                raise ValueError("persona has opening '---' but no closing '---' fence")
            fm_text = text[fm_start:close]
            # Body starts after the closing fence + its newline
            body_start = text.find("\n", close + 4)
            body = text[body_start + 1 :] if body_start != -1 else ""
            fm = yaml.safe_load(fm_text) or {}
            if not isinstance(fm, dict):
                raise ValueError(
                    f"persona frontmatter must be a YAML mapping, got {type(fm).__name__}"
                )
        else:
            fm = {}
            body = text
        name = fm.get("name") or fallback_name
        if not name:
            raise ValueError("persona has no 'name' in frontmatter and no fallback was given")
        return cls(
            name=str(name),
            description=str(fm.get("description", "")),
            body=body.strip(),
            voice=fm.get("voice"),
            metadata=fm.get("metadata") or {},
        )

    @classmethod
    def from_file(cls, path: Path | str) -> Persona:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"persona file not found: {path}")
        return cls.from_text(path.read_text(encoding="utf-8"), fallback_name=path.stem)

    def to_prompt_text(self, frame: dict[str, Any] | None = None) -> str:
        """Render the persona as the text a client should inject as system prompt.

        Combines the body with a short header carrying name and voice so the
        identity reads as deliberate, not as raw markdown dropped in.

        If ``frame`` is provided, the body is interpolated against it before
        rendering. Placeholders use ``{path.to.value}`` syntax; unknown paths
        render as empty strings. Pass ``frame=None`` (the default) when
        rendering for static use (mount instructions); pass the live frame
        dict when rendering for a session-aware prompt fetch.
        """
        rendered_body = render_with_frame(self.body, frame)
        lines: list[str] = [f"You are acting as the persona '{self.name}'."]
        if self.description:
            lines.append(self.description)
        if self.voice:
            lines.append(f"Voice: {self.voice}.")
        lines.extend(("", rendered_body))
        return "\n".join(lines).strip()


PersonaSource = str | Path | dict[str, str] | list["Persona"] | None


def load_personas(source: PersonaSource) -> dict[str, Persona]:
    """Load personas from one of several source shapes.

    Accepts:
    - ``None`` -> empty dict
    - A directory path (str or Path) -> parses every ``*.md`` file
    - A dict ``{name: text}`` -> parses each text; key wins as fallback name
    - A list of pre-built ``Persona`` objects

    Raises if the directory does not exist or any file fails to parse.
    """
    if source is None:
        return {}
    if isinstance(source, list):
        return {p.name: p for p in source}
    if isinstance(source, dict):
        out: dict[str, Persona] = {}
        for fallback_name, text in source.items():
            p = Persona.from_text(text, fallback_name=fallback_name)
            if p.name in out:
                raise ValueError(
                    f"duplicate persona name {p.name!r} when loading from dict; "
                    f"two entries claim that name"
                )
            out[p.name] = p
        return out
    path = Path(source).expanduser()
    if path.is_file():
        # single PERSONA.md
        p = Persona.from_file(path)
        return {p.name: p}
    if not path.is_dir():
        raise ValueError(
            f"personas source must be a directory, a file, a dict, or a list; "
            f"got {source!r} which exists={path.exists()}"
        )
    dir_out: dict[str, Persona] = {}
    for f in sorted(path.glob("*.md")):
        p = Persona.from_file(f)
        if p.name in dir_out:
            raise ValueError(
                f"duplicate persona name {p.name!r} in {path}: "
                f"both {f} and a prior file share that name"
            )
        dir_out[p.name] = p
    return dir_out


def resolve_default(personas: dict[str, Persona], requested: str | None) -> Persona | None:
    """Pick the default persona for a session.

    Honors ``requested`` if given; raises if the name does not exist. If no
    request is given, returns the lexically first persona by name as a
    deterministic default; this beats an arbitrary insertion-order default
    and means swapping ``default_persona`` between releases is intentional.
    Returns ``None`` if no personas are loaded.
    """
    if not personas:
        return None
    if requested is not None:
        if requested not in personas:
            raise ValueError(f"default_persona={requested!r} not found; loaded: {sorted(personas)}")
        return personas[requested]
    return personas[min(personas)]
