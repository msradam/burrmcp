"""Assembly: a bundled, mountable agent surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from burr.core import Application
    from fastmcp import FastMCP

    from theodosia.persona import PersonaSource


@dataclass(frozen=True)
class Assembly:
    """A bundled, mountable agent surface.

    ``workflow`` is the Burr ``Application``, a factory, or a ``module:attr``
    import string. Everything else is optional configuration the mount
    layer reads.
    """

    name: str
    workflow: Application | Callable[[], Application] | str
    version: str = "0.1.0"
    personas: PersonaSource = None
    default_persona: str | None = None
    upstream: dict[str, Any] | None = None
    instructions: str | None = None
    include_default_instructions: bool = True
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def serve(self, **overrides: Any) -> FastMCP:
        from theodosia.adapter import mount

        return mount(self, **overrides)

    def with_overrides(self, **overrides: Any) -> Assembly:
        return replace(self, **overrides)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Assembly:
        return cls(**data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Assembly:
        import yaml

        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"assembly YAML must be a mapping; got {type(data).__name__}")
        return cls.from_dict(data)

    def to_yaml(self, path: str | Path | None = None) -> str:
        """Serialize this assembly to YAML. Writes to ``path`` if given; always returns the text.

        Non-YAML-serializable fields (a built ``Application``, a callable factory)
        cannot round-trip. Use a ``module:attr`` import string for ``workflow``
        when you want a fully declarative artifact.
        """
        import yaml

        data = self.to_dict()
        text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        if path is not None:
            Path(path).expanduser().write_text(text, encoding="utf-8")
        return text
