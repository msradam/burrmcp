"""``theodosia serve``, ``doctor``, ``ui``, plus the ``build_cli`` factory and ``main``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from theodosia.adapter import ServingMode, mount
from theodosia.cli._branding import (
    _BRANDING,
    _DEFAULT_HELP,
    _Branding,
    _set_branding,
    console,
    err_console,
)
from theodosia.cli._resolve import _resolve_serve_target
from theodosia.cli._topology import render
from theodosia.cli.reports import report
from theodosia.cli.sessions import logs, sessions_ls, sessions_show, sessions_tail, watch
from theodosia.cli.status import status, verify
from theodosia.primer import primer


def serve(
    target: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Import target in module:attr form. The attr is either a "
                "burr.core.Application or a callable returning one. Optional "
                "when a graph is baked in via build_cli(application=...)."
            ),
        ),
    ] = None,
    mode: Annotated[
        ServingMode,
        typer.Option("--mode", help="Serving mode.", case_sensitive=False),
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
                "Extra directory to prepend to sys.path before importing. "
                "Repeatable. Use when the FSM module is in a subdirectory."
            ),
        ),
    ] = None,
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="Transport: stdio (default), http, sse, or streamable-http.",
            case_sensitive=False,
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address for http/sse transports."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="Port for http/sse transports."),
    ] = 8000,
) -> None:
    """Launch an importable Burr Application or factory as an MCP server."""
    application_or_factory, derived_name = _resolve_serve_target(target, app_dir or [])
    server = mount(
        application_or_factory,
        mode=mode,
        name=name or derived_name,
        upstream=_BRANDING.upstream,
    )
    transport_norm = transport.lower()
    if transport_norm == "stdio":
        server.run(transport="stdio")
    elif transport_norm == "http":
        server.run(transport="http", host=host, port=port)
    elif transport_norm == "sse":
        server.run(transport="sse", host=host, port=port)
    elif transport_norm == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        raise typer.BadParameter(
            f"unknown transport {transport!r}; expected stdio, http, sse, or streamable-http"
        )


def doctor(
    target: Annotated[
        str | None,
        typer.Argument(help="Import target in module:attr form. Same shape as `serve`."),
    ] = None,
    app_dir: Annotated[
        list[str] | None,
        typer.Option("--app-dir", help="Extra sys.path directory before importing."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print every check, not just failures and warnings."),
    ] = False,
    runtime: Annotated[
        bool,
        typer.Option(
            "--runtime",
            help=(
                "Also mount the server in-process and probe its wire shape: "
                "tool listing, resource catalog, step result content blocks."
            ),
        ),
    ] = False,
) -> None:
    """Statically validate a Burr Application or factory before mounting."""
    from theodosia.doctor import format_report, run_checks

    application_or_factory, _ = _resolve_serve_target(target, app_dir or [])
    report_obj = run_checks(application_or_factory, runtime=runtime)
    typer.echo(format_report(report_obj, verbose=verbose))
    if not report_obj.ok:
        raise typer.Exit(code=1)


def ui(
    port: Annotated[int, typer.Option("--port", help="Port for the Burr UI server.")] = 7241,
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address. Use 0.0.0.0 to expose on the network."),
    ] = "127.0.0.1",
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open a browser tab when the UI starts.")
    ] = False,
) -> None:
    """Launch the Burr UI to inspect tracked sessions.

    Prefers the local install if apache-burr\\[start] is present (one
    process). Otherwise shells out to ``uvx --from 'apache-burr\\[start]'``.
    """
    import shutil
    import subprocess

    forwarded = ["--port", str(port), "--host", host]
    if no_open:
        forwarded.append("--no-open")

    try:
        import loguru  # noqa: F401 (probe-only)

        cmd = [
            sys.executable,
            "-c",
            "from burr.cli.__main__ import cli_run_server; cli_run_server()",
            *forwarded,
        ]
    except ImportError:
        if shutil.which("uvx") is None:
            err_console.print(
                "the Burr UI needs either apache-burr[start] installed in the "
                f"current env (try [bold]uv pip install '{_BRANDING.ui_extra}'[/]) or "
                "[bold]uvx[/] on PATH (https://docs.astral.sh/uv/) for one-shot bootstrap."
            )
            raise typer.Exit(code=1) from None
        cmd = ["uvx", "--from", "apache-burr[start]", "burr", *forwarded]

    console.print(f"Launching Burr UI on [link]http://{host}:{port}[/link]")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(code=exc.returncode or 1) from exc
    except KeyboardInterrupt:
        pass


def _version_callback(value: bool) -> None:
    if not value:
        return
    from importlib.metadata import PackageNotFoundError, version

    prog = _BRANDING.prog_name
    pkg = prog if prog != "theodosia" else "theodosia"
    try:
        v = version(pkg)
    except PackageNotFoundError:
        try:
            v = version("theodosia")
        except PackageNotFoundError:
            v = "unknown"
    console.print(f"{prog} {v}")
    raise typer.Exit()


def build_cli(
    prog_name: str = "theodosia",
    *,
    application: Any | None = None,
    help: str | None = None,
    server_name: str | None = None,
    ui_extra: str = "theodosia[ui]",
    home: str | Path | None = None,
    upstream: dict[str, Any] | None = None,
) -> typer.Typer:
    """Build a theodosia CLI, optionally rebranded for a downstream package.

    A package that ships its own MCP graph can expose its own command::

        # my_fsm_mcp/cli.py
        from theodosia.cli import build_cli, run
        from my_fsm_mcp import build_application

        cli = build_cli("my-fsm-mcp", application=build_application,
                        help="My graph as an MCP server.")

        def main() -> int:
            return run(cli)

    Then ``my-fsm-mcp serve`` (no target needed), ``my-fsm-mcp doctor``, and
    ``my-fsm-mcp sessions ls`` all carry the downstream's name. Sessions are
    still stored in Burr's tracker format; set ``home`` to match the
    ``storage_dir`` the downstream's ``LocalTrackingClient`` writes to.

    Args:
        prog_name: command name shown in help and used as the default
            server name when a baked-in Application has no other name.
        application: an ``Application``, a factory, or a ``module:attr``
            string. When set, ``serve``/``doctor`` accept no target.
        help: root help text. Defaults to the theodosia description.
        server_name: default MCP server name surfaced to clients.
        ui_extra: pip extra named in the ``ui`` install hint.
        home: default tracker storage root for the observability
            commands. Overridden per-invocation by ``--home``.
        upstream: map of server name to a ``fastmcp.Client`` transport.
            Action bodies reach these other MCP servers with
            ``call_upstream(server, tool, args)``. Passed through to
            ``mount`` by ``serve``.
    """
    _set_branding(
        _Branding(
            prog_name=prog_name,
            application=application,
            server_name=server_name,
            ui_extra=ui_extra,
            home=home,
            upstream=upstream,
        )
    )

    cli = typer.Typer(
        name=prog_name,
        help=help or _DEFAULT_HELP,
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
    )
    sessions = typer.Typer(
        name="sessions",
        help="Inspect Burr tracker storage: list, show, or live-tail a session.",
        pretty_exceptions_enable=False,
        no_args_is_help=True,
    )
    sessions.command("ls")(sessions_ls)
    sessions.command("list", hidden=True)(sessions_ls)  # muscle-memory alias
    sessions.command("show")(sessions_show)
    sessions.command("tail")(sessions_tail)
    cli.add_typer(sessions, name="sessions")

    cli.command()(serve)
    cli.command()(doctor)
    cli.command()(render)
    cli.command()(ui)
    cli.command()(watch)
    cli.command()(logs)
    cli.command()(verify)
    cli.command()(status)
    cli.command()(report)
    # ``primer`` is a Theodosia-specific demo of Theodosia's onboarding and
    # ships labels and URLs that name the project explicitly. Don't register
    # it for rebranded CLIs ( ``build_cli(prog_name="my-fsm")`` ) since the
    # downstream surface would otherwise advertise theodosia.
    if prog_name == "theodosia":
        cli.command()(primer)

    @cli.callback()
    def _root(
        version: bool = typer.Option(
            False,
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ) -> None:
        pass

    return cli


def run(cli: typer.Typer, argv: list[str] | None = None) -> int:
    """Run a Typer app with graceful exit-code handling. ``argv`` is for tests."""
    try:
        rv = cli(args=argv, standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except typer.Exit as e:
        return e.exit_code or 0
    except SystemExit as e:
        if e.code is None:
            return 0
        if isinstance(e.code, int):
            return e.code
        err_console.print(str(e.code))
        return 1
