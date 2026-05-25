"""SPIKE: a single-file ``.app`` format for Theodosia.

A ``.app`` file is YAML frontmatter (agent metadata + mount config) followed by
a Python body that defines ``build_application`` (or ``application``). One
portable artifact carries both the workflow and how to serve it, the way the
landing page's ``incident.app`` panel implies. Theodosia loads it, reads the
frontmatter for mount options, and execs the body to get the Burr Application.

    theodosia serve incident.app      # (sketch) detect .app, load, mount

Security note: loading a ``.app`` execs arbitrary Python, so it is a
trusted-local-file feature, not "fetch and run an .app from the internet."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # frontmatter is tiny; fall back to a minimal parser
    yaml = None


def _parse_frontmatter(block: str) -> dict[str, Any]:
    if yaml is not None:
        return yaml.safe_load(block) or {}
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta


def parse_appfile(text: str) -> tuple[dict[str, Any], str]:
    """Split a .app into (frontmatter dict, python source). Frontmatter is the
    block between the first two `---` fences; everything after is Python."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            fm = _parse_frontmatter("\n".join(lines[1:end]))
            return fm, "\n".join(lines[end + 1 :])
    return {}, text


# Frontmatter keys that map straight to mount() kwargs (all data, YAML-safe).
_MOUNT_KEYS = (
    "name",
    "instructions",
    "session_ttl_seconds",
    "max_sessions",
    "action_timeout_seconds",
    "upstream",
    "external_tools",
)
# mount() kwargs that are callables/objects, so they live in the Python body and
# are picked up by name if defined there.
_BODY_KEYS = ("input_validators", "next_hint", "state_loader")


def load_app(path: str | Path) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Return (build_application callable or Application, frontmatter meta,
    the exec'd namespace so the caller can pick up body-defined callables)."""
    p = Path(path)
    meta, code = parse_appfile(p.read_text(encoding="utf-8"))
    ns: dict[str, Any] = {"__file__": str(p), "__name__": p.stem}
    exec(compile(code, str(p), "exec"), ns)  # noqa: S102 (trusted local file)
    target = ns.get("build_application") or ns.get("application")
    if target is None:
        raise ValueError(f"{p}: no build_application or application defined")
    return target, meta, ns


def mount_kwargs(meta: dict[str, Any], ns: dict[str, Any]) -> dict[str, Any]:
    """Assemble mount() kwargs: data from the frontmatter, callables from the
    Python body. `description` is accepted as an alias for `instructions`."""
    kwargs = {k: meta[k] for k in _MOUNT_KEYS if k in meta}
    if "instructions" not in kwargs and "description" in meta:
        kwargs["instructions"] = meta["description"]
    for k in _BODY_KEYS:
        if k in ns:
            kwargs[k] = ns[k]
    return kwargs


def serve_appfile(path: str | Path):
    """Load a .app and mount it with Theodosia, carrying the full MCP-server
    config from the frontmatter (and any callables from the body)."""
    from theodosia import mount

    target, meta, ns = load_app(path)
    return mount(target, **mount_kwargs(meta, ns))
