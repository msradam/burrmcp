"""Theodosia design tokens, single source of truth.

These tokens drive three surfaces, so they must come from one place:

- The docs site CSS (``website/src/styles/theodosia.css``)
- The themed Burr web UI overlay shipped by ``theodosia ui``
- The Theodosia TUI palette shipped by ``theodosia tui``

Light mode is the canonical surface for the docs and the web UI overlay. Dark
mode is the canonical surface for the TUI (which lives in terminals where dark
is the prevailing context). Both modes share the same accent (``pine``) which
carries the brand.

Usage::

    from theodosia.tokens import LIGHT, DARK, as_css_variables, as_python_dict

    css = as_css_variables(LIGHT)              # ":root { --thd-pine: #573e8a; ... }"
    palette = as_python_dict(DARK)             # {"pine": "#c4a7e7", ...}

If you add, rename, or recolor a token, update ``website/src/styles/theodosia.css``
in the same change (it still hand-mirrors these values until a build step lifts
the duplication).
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Tokens:
    """The set of design tokens both modes carry.

    Field names are snake_case here; the CSS helper renders them as kebab-case
    (``--thd-pine-soft``) and the dict helper keeps the snake_case form (used
    by Python consumers like the TUI palette).
    """

    # surface
    paper: str
    sheet: str
    raised: str
    edge: str
    rule: str

    # ink
    ink: str
    subtle: str
    muted: str

    # accent (pine = the brand, carried across modes)
    pine: str
    pine_soft: str
    pine_tint: str

    # status
    ok: str
    err: str
    running: str

    # font stack (shared across modes; mode-specific surfaces use the same fonts).
    # Names are deliberately short (``display``/``sans``/``mono``) so the CSS
    # vars come out as ``--thd-display`` etc. to match the existing docs and
    # web UI overlay conventions.
    display: str = '"Inria Serif", "Iowan Old Style", Georgia, serif'
    sans: str = '"Funnel Sans", -apple-system, system-ui, "Segoe UI", Roboto, sans-serif'
    mono: str = '"JetBrains Mono", ui-monospace, "SF Mono", Consolas, monospace'


LIGHT = Tokens(
    paper="#fafafa",
    sheet="#ffffff",
    raised="#f5f5f3",
    edge="#e6e6e2",
    rule="#d4d4cf",
    ink="#0a0a0a",
    subtle="#3a3a38",
    muted="#7a7a76",
    pine="#286983",
    pine_soft="#3d8aa3",
    pine_tint="#eaf3f6",
    ok="#5b8d68",
    err="#b4637a",
    running="#ea9d34",
)


DARK = Tokens(
    paper="#10131c",
    sheet="#161a23",
    raised="#1d212d",
    edge="#2a2f3c",
    rule="#3a4050",
    ink="#e0def4",
    subtle="#c4c2e0",
    muted="#6e6a86",
    pine="#9ccfd8",
    pine_soft="#56949f",
    pine_tint="#1a2a32",
    ok="#9ccfd8",
    err="#eb6f92",
    running="#f6c177",
)


def as_css_variables(tokens: Tokens, *, prefix: str = "thd") -> str:
    """Render tokens as a ``:root { --<prefix>-name: value; ... }`` block.

    Used by the themed Burr web UI overlay (where it gets served as a CSS file
    after being patched into ``index.html``) and intended for the docs site
    build step.
    """
    lines = [":root {"]
    for f in fields(tokens):
        kebab = f.name.replace("_", "-")
        lines.append(f"  --{prefix}-{kebab}: {getattr(tokens, f.name)};")
    lines.append("}")
    return "\n".join(lines)


def as_python_dict(tokens: Tokens) -> dict[str, str]:
    """Render tokens as a flat dict.

    Used by the TUI's ``PALETTE`` and anywhere else Python wants direct token
    access without going through CSS.
    """
    return {f.name: getattr(tokens, f.name) for f in fields(tokens)}
