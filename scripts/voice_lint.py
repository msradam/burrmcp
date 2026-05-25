"""Voice lint for authored prose.

Enforces the CLAUDE.md voice rules across the repo: no em dashes, no
marketing adjectives, no slogan voice, no AI attribution. Designed to
be cheap to run in a git pre-commit hook.

Usage:
    python scripts/voice_lint.py             # scan repo defaults
    python scripts/voice_lint.py PATH...     # scan only these paths
    python scripts/voice_lint.py --staged    # scan only files staged
                                             # for the next commit
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths scanned by default. Subset to the surfaces we author: source,
# tests, examples, top-level docs, and pyproject. Bench / lockfiles /
# vendored content stays out.
_DEFAULT_GLOBS: tuple[str, ...] = (
    "*.md",
    "website/src/content/docs/**/*.md",
    "src/**/*.py",
    "tests/**/*.py",
    "examples/**/*.py",
    "pyproject.toml",
)

# Paths excluded even if matched by a default glob. Third-party SKILL
# content is preserved verbatim per CLAUDE.md.
_EXCLUDED_PARTS: tuple[str, ...] = (
    "examples/skills/",
    "examples/data/",
    ".venv/",
    "__pycache__/",
    ".git/",
    ".ruff_cache/",
)

# Specific files exempted: CLAUDE.md catalogs the banned words as data
# (it documents the rules); scanning it would always be self-referential.
_EXCLUDED_FILES: frozenset[str] = frozenset({"CLAUDE.md"})


class Violation:
    __slots__ = ("lineno", "path", "rule", "snippet")

    def __init__(self, path: Path, lineno: int, rule: str, snippet: str) -> None:
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.snippet = snippet

    def format(self, root: Path) -> str:
        rel = self.path.relative_to(root) if self.path.is_absolute() else self.path
        return f"{rel}:{self.lineno}: [{self.rule}] {self.snippet.rstrip()}"


# Em dash and en dash. Anywhere we author prose. CLAUDE.md is explicit.
# The en dash inside the character class is intentional (we're matching it).
_EM_DASH_RE = re.compile(r"[–—]")  # noqa: RUF001

# Marketing adjectives that signal hype rather than describe behavior.
# Word-boundary anchored so "robustly" doesn't match while "robust" does.
_MARKETING_WORDS = (
    "blazing",
    "blazingly",
    "powerful",
    "robust",
    "comprehensive",
    "production-grade",
    "production-ready",
    "enterprise-grade",
    "first-class",
    "world-class",
    "state-of-the-art",
    "cutting-edge",
    "seamless",
    "seamlessly",
    "delightful",
    "lightning-fast",
    "battle-tested",
)
_MARKETING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _MARKETING_WORDS) + r")\b",
    re.IGNORECASE,
)

# Phrases that flag opening hype or vendor-pitch voice.
_HYPE_PHRASES = (
    "we believe",
    "introducing ",
    "the magic of",
    "it just works",
    "out of the box",
)
_HYPE_RE = re.compile(
    "|".join(re.escape(p) for p in _HYPE_PHRASES),
    re.IGNORECASE,
)

# AI co-author / generated-by trailers.
_AI_ATTRIBUTION_RE = re.compile(
    r"(?:Co-Authored-By:\s*Claude"
    r"|Generated with \[?Claude Code"
    r"|\xf0\x9f\xa4\x96 Generated"  # robot emoji + Generated
    r"|Claude added this"
    r"|per user request"
    r")",
    re.IGNORECASE,
)


def _is_excluded(path: Path) -> bool:
    text = str(path)
    if any(part in text for part in _EXCLUDED_PARTS):
        return True
    return path.name in _EXCLUDED_FILES


def _default_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for glob in _DEFAULT_GLOBS:
        files.extend(p for p in root.glob(glob) if p.is_file())
    return sorted(p for p in files if not _is_excluded(p))


def _staged_files(root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=root,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    files = [root / line.strip() for line in out.splitlines() if line.strip()]
    return [p for p in files if p.is_file() and not _is_excluded(p)]


def _scan(text: str, path: Path) -> Iterable[Violation]:
    for i, line in enumerate(text.splitlines(), start=1):
        if _EM_DASH_RE.search(line):
            yield Violation(path, i, "em-dash", line)
        if _MARKETING_RE.search(line):
            yield Violation(path, i, "marketing-word", line)
        if _HYPE_RE.search(line):
            yield Violation(path, i, "hype-phrase", line)
        if _AI_ATTRIBUTION_RE.search(line):
            yield Violation(path, i, "ai-attribution", line)


def lint(paths: Iterable[Path]) -> list[Violation]:
    violations: list[Violation] = []
    # Self-exempt: this file mentions every banned word as data, which
    # would trigger every rule against itself.
    self_path = Path(__file__).resolve()
    for path in paths:
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        violations.extend(_scan(text, path))
    return violations


def main(argv: list[str]) -> int:
    if "--staged" in argv:
        files = _staged_files(REPO_ROOT)
        if not files:
            return 0
    elif len(argv) > 1:
        files = [Path(a).resolve() for a in argv[1:]]
        files = [f for f in files if f.is_file() and not _is_excluded(f)]
    else:
        files = _default_files(REPO_ROOT)

    violations = lint(files)
    if not violations:
        return 0

    print(f"voice lint: {len(violations)} violation(s)", file=sys.stderr)
    for v in violations:
        print(v.format(REPO_ROOT), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
