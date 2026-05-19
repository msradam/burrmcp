"""``burr-mcp`` CLI: launch or validate an importable Burr Application.

Usage:

    burr-mcp serve coffee_order:build_application --mode step
    burr-mcp serve mymodule:application_factory --mode dynamic --name coffee
    burr-mcp doctor coffee_order:build_application

The ``module:attr`` syntax matches uvicorn / gunicorn conventions. The
referenced attribute can be either a built ``Application`` (shared
across sessions) or a callable factory returning one (per-session
isolation). See ``burr_mcp.mount`` for the distinction. The ``doctor``
subcommand runs static validation against the resolved Application
before you mount it.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Any

from burr_mcp.adapter import ServingMode, mount


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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="burr-mcp",
        description="Mount a Burr Application as an MCP server.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve",
        help="Launch an importable Burr Application or factory as an MCP server.",
    )
    serve.add_argument(
        "target",
        help="Import target in module:attr form. The attr is either a "
        "burr.core.Application or a callable returning one.",
    )
    serve.add_argument(
        "--mode",
        choices=[m.value for m in ServingMode],
        default=ServingMode.STEP.value,
        help="Serving mode (default: step).",
    )
    serve.add_argument(
        "--name",
        default=None,
        help="MCP server name surfaced to clients (default: derived from target).",
    )
    serve.add_argument(
        "--app-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="Extra directory to prepend to sys.path before importing the target. "
        "Repeatable. Use this when your FSM module lives in a subdirectory of "
        "the project (e.g. --app-dir ./examples).",
    )

    doctor = sub.add_parser(
        "doctor",
        help="Statically validate a Burr Application or factory before mounting.",
    )
    doctor.add_argument(
        "target",
        help="Import target in module:attr form. Same shape as `serve`.",
    )
    doctor.add_argument(
        "--app-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="Extra directory to prepend to sys.path before importing the target.",
    )
    doctor.add_argument(
        "--verbose",
        action="store_true",
        help="Print message and details for every check, not just failures and warnings.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        application_or_factory = _import_target(args.target, args.app_dir)
        server_name = args.name or args.target.split(":", 1)[0].split(".")[-1]
        server = mount(
            application_or_factory,
            mode=ServingMode(args.mode),
            name=server_name,
        )
        # FastMCP.run() handles stdio by default; HTTP/SSE transports
        # are configured at the FastMCP layer if needed (out of scope
        # for v0.1).
        server.run()
        return 0

    if args.command == "doctor":
        from burr_mcp.doctor import format_report, run_checks

        application_or_factory = _import_target(args.target, args.app_dir)
        report = run_checks(application_or_factory)
        print(format_report(report, verbose=args.verbose))
        return 0 if report.ok else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
