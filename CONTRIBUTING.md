# Contributing to Theodosia

Thanks for your interest. Theodosia is a thin adapter that mounts
[Apache Burr](https://github.com/apache/burr) Applications as
[FastMCP](https://github.com/jlowin/fastmcp) servers, so most contributions are
small and focused.

## Development setup

Theodosia uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/msradam/theodosia
cd theodosia
uv sync
```

## Quality gates (the same ones CI runs)

Run these before opening a pull request; CI runs them on Python 3.11 to 3.13 and
they must pass:

```bash
uv run ruff format .            # format
uv run ruff check .             # lint
uv run mypy src/theodosia       # type-check
uv run pytest                   # tests (574+; smoke tests are opt-in: -m smoke)
uv run bandit -r src/theodosia --severity-level medium   # SAST
uvx pip-audit                   # dependency vulnerabilities
```

## Conventions

- The four-tool STEP surface and the Burr `Application` boundary are the stable
  architecture. New capability should pass through `mount()`, not widen the tool
  surface.
- Tests are required for behavior changes. The suite is hermetic: demos that
  call an LLM or shell out have a monkeypatchable indirection, so the suite runs
  without a model runtime or network.
- Voice: plain declarative prose, no em dashes, no marketing adjectives, no AI
  co-author trailers in commits or PRs.
- Keep comments to the non-obvious "why"; let names carry the "what".

## Pull requests

Describe the change and why. Link an issue if one exists. CI must be green, and a
maintainer review is required before merge. By contributing you agree your work
is licensed under Apache 2.0.

## Security

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
