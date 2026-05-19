"""``burr-mcp`` CLI: launch or validate an importable Burr Application.

Built with Typer, so ``burr-mcp --help`` and ``burr-mcp <subcommand>
--help`` render structured help with the option types, defaults, and
short descriptions baked in.

Usage:

    burr-mcp serve coffee_order:build_application --mode step
    burr-mcp serve mymodule:application_factory --mode dynamic --name coffee
    burr-mcp doctor coffee_order:build_application

The ``module:attr`` syntax matches uvicorn / gunicorn conventions. The
referenced attribute is either a built ``burr.core.Application``
(shared across sessions) or a callable factory returning one (one
build per session for state isolation). See ``burr_mcp.mount`` for
the distinction. The ``doctor`` subcommand runs static validation
against the resolved Application before you mount it.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Annotated, Any

import typer

from burr_mcp.adapter import ServingMode, mount

app = typer.Typer(
    name="burr-mcp",
    help="Mount a Burr Application as an MCP server.",
    no_args_is_help=True,
    add_completion=False,
)


def _import_target(target: str, extra_paths: list[str] | None = None) -> Any:
    """Resolve ``module:attr`` into a Python object.

    Prepends the current working directory and any ``extra_paths`` to
    ``sys.path`` so users can point at their own modules from a checkout
    (the convention uvicorn / gunicorn follow). Raises ``SystemExit``
    with a clear message on import or attribute failure so the CLI
    doesn't dump a stack trace at the user.
    """
    if ":" not in target:
        raise SystemExit(
            f"target must be of the form module:attr (got {target!r}). "
            f"Example: coffee_order:build_application"
        )
    paths = [os.getcwd(), *(extra_paths or [])]
    for p in paths:
        absp = os.path.abspath(p)
        if absp not in sys.path:
            sys.path.insert(0, absp)
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import module {module_name!r}: {exc}") from exc
    if not hasattr(module, attr):
        available = ", ".join(sorted(n for n in dir(module) if not n.startswith("_")))
        raise SystemExit(
            f"module {module_name!r} has no attribute {attr!r}. "
            f"Available top-level names: {available}"
        )
    return getattr(module, attr)


@app.command()
def serve(
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "Import target in module:attr form. The attr is either a "
                "burr.core.Application or a callable returning one."
            ),
        ),
    ],
    mode: Annotated[
        ServingMode,
        typer.Option(
            "--mode",
            help="Serving mode.",
            case_sensitive=False,
        ),
    ] = ServingMode.STEP,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="MCP server name surfaced to clients (default: derived from target).",
        ),
    ] = None,
    app_dir: Annotated[
        list[str] | None,
        typer.Option(
            "--app-dir",
            help=(
                "Extra directory to prepend to sys.path before importing the target. "
                "Repeatable. Use this when your FSM module lives in a subdirectory "
                "of the project (e.g. --app-dir ./examples)."
            ),
        ),
    ] = None,
) -> None:
    """Launch an importable Burr Application or factory as an MCP server."""
    application_or_factory = _import_target(target, app_dir or [])
    server_name = name or target.split(":", 1)[0].split(".")[-1]
    server = mount(
        application_or_factory,
        mode=mode,
        name=server_name,
    )
    server.run()


@app.command()
def doctor(
    target: Annotated[
        str,
        typer.Argument(help="Import target in module:attr form. Same shape as `serve`."),
    ],
    app_dir: Annotated[
        list[str] | None,
        typer.Option(
            "--app-dir",
            help="Extra directory to prepend to sys.path before importing the target.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print message and details for every check, not just failures and warnings.",
        ),
    ] = False,
) -> None:
    """Statically validate a Burr Application or factory before mounting."""
    from burr_mcp.doctor import format_report, run_checks

    application_or_factory = _import_target(target, app_dir or [])
    report = run_checks(application_or_factory)
    typer.echo(format_report(report, verbose=verbose))
    if not report.ok:
        raise typer.Exit(code=1)


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``argv`` is for testing; ``None`` lets Typer read
    ``sys.argv`` normally."""
    try:
        rv = app(args=argv, standalone_mode=False)
        # With ``standalone_mode=False`` Typer/Click returns the exit
        # code from any in-callback ``typer.Exit(code=N)`` as the call's
        # return value rather than raising. Pass it through.
        return rv if isinstance(rv, int) else 0
    except typer.Exit as e:
        return e.exit_code or 0
    except SystemExit as e:
        # ``_import_target`` raises ``SystemExit(message)`` with a string
        # code on import/attribute failure. Surface the message to stderr
        # and return a clean nonzero so callers see a structured error
        # rather than a stack trace.
        if e.code is None:
            return 0
        if isinstance(e.code, int):
            return e.code
        print(e.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
