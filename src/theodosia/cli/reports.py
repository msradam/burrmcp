"""``theodosia report``: markdown post-mortem of a session, optional webhook POST."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from theodosia.cli._branding import err_console
from theodosia.cli._resolve import _resolve_app, _resolve_home
from theodosia.cli._steps import (
    StepRow,
    _read_refusals,
    _read_steps,
    _terminal_state_may_be_stale,
)


def report(
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id (full uuid or prefix). Defaults to most recent."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name. Defaults to most recent."),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home", help="Tracker storage root. Overrides the CLI default (see --help)."
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Write the report to a file. Default: print to stdout.",
        ),
    ] = None,
    webhook: Annotated[
        str | None,
        typer.Option(
            "--webhook",
            help=(
                "POST the report as application/markdown to this URL. "
                "Useful for Slack/Discord/Linear/PagerDuty hooks. The post "
                "happens in addition to --out / stdout, not instead."
            ),
        ),
    ] = None,
) -> None:
    """Generate a markdown post-mortem for a tracked session.

    The report covers session metadata, an ordered timeline of every action
    (with status and any error summary), refusals (blocked transitions the
    agent attempted), and the final state of the FSM. Compatible with any
    Burr session theodosia has tracked; reads from ``~/.theodosia`` (or
    ``--home``) without requiring the original mounted server to be
    running. Optionally POSTs the rendered markdown to a webhook for
    Slack, Discord, Linear, PagerDuty, or any inbox that accepts
    ``application/markdown``.
    """
    resolved_home = _resolve_home(home)
    log_path, proj, aid = _resolve_app(resolved_home, project, app_id)
    steps = _read_steps(log_path)
    refusals = _read_refusals(log_path)
    markdown = _render_session_report(
        project=proj, app_id=aid, log_path=log_path, steps=steps, refusals=refusals
    )

    if out is not None:
        out.expanduser().write_text(markdown, encoding="utf-8")
        err_console.print(
            Text.assemble(
                ("wrote report: ", "muted"),
                (str(out.expanduser()), "ok"),
            )
        )
    else:
        print(markdown)

    if webhook is not None:
        _post_report(webhook, markdown, project=proj, app_id=aid)


def _render_session_report(
    *,
    project: str,
    app_id: str,
    log_path: Path,
    steps: list[StepRow],
    refusals: list[StepRow],
) -> str:
    """Render a markdown post-mortem from a session's step rows and refusals."""
    lines: list[str] = [
        f"# Session report: `{project}` / `{app_id}`",
        "",
        f"- Log path: `{log_path}`",
        f"- Steps recorded: {len(steps)}",
        f"- Refusals recorded: {len(refusals)}",
    ]
    if steps:
        first = steps[0]
        last = steps[-1]
        total_ms = sum(s.duration_ms or 0 for s in steps)
        lines.extend(
            (
                f"- First action: `{first.action}` at {first.started}",
                f"- Last action: `{last.action}` ({last.status}) at {last.started}",
                f"- Total action duration: {total_ms:.1f} ms",
            )
        )
    lines.append("")

    if steps:
        lines.extend(
            (
                "## Timeline",
                "",
                "| seq | action | status | started | dur (ms) | error |",
                "|---:|---|---|---|---:|---|",
            )
        )
        for s in steps:
            dur = f"{s.duration_ms:.1f}" if s.duration_ms is not None else "-"
            err = (s.error_summary or "").replace("|", "\\|")[:80]
            lines.append(f"| {s.seq} | `{s.action}` | {s.status} | {s.started} | {dur} | {err} |")
        lines.append("")

    if refusals:
        lines.extend(
            (
                "## Refusals",
                "",
                "Refusals are transitions the agent attempted that the FSM rejected. "
                "They never advanced state; they are recorded here for postmortem.",
                "",
                "| seq | action | ts | reason |",
                "|---:|---|---|---|",
            )
        )
        for r in refusals:
            reason = (r.error_summary or "").replace("|", "\\|")[:80]
            lines.append(f"| {r.seq} | `{r.action}` | {r.started} | {reason} |")
        lines.append("")

    if steps:
        final_state = steps[-1].state_summary
        if final_state:
            lines.extend(("## Final state", ""))
            if _terminal_state_may_be_stale(steps):
                lines.extend(
                    (
                        "> Note: the terminal action's post-state cannot be read back "
                        "from Burr's tracker when the action body is sync (Burr fires "
                        "`post_run_step` with pre-action state in that case). The "
                        "snapshot below is one step behind for a sync terminal; for "
                        "the true post-action state read `theodosia://state` from a "
                        "live session or write the action body `async def`.",
                        "",
                    )
                )
            lines.extend(
                (
                    "```json",
                    json.dumps(final_state, indent=2, default=str),
                    "```",
                    "",
                )
            )

    if not steps and not refusals:
        lines.extend(("_(no steps or refusals recorded at this session yet)_", ""))

    return "\n".join(lines)


def _post_report(webhook_url: str, markdown: str, *, project: str, app_id: str) -> None:
    """POST the rendered report to a webhook as ``application/markdown``.

    Only http and https URLs are accepted; file:// or custom schemes would
    let a tampered config exfiltrate report contents to a local path or a
    side channel.
    """
    import urllib.parse
    import urllib.request

    scheme = urllib.parse.urlparse(webhook_url).scheme.lower()
    if scheme not in ("http", "https"):
        err_console.print(
            Text.assemble(
                ("refused webhook ", "err"),
                (webhook_url, "subtle"),
                (f" (only http/https allowed; got {scheme!r})", "muted"),
            )
        )
        return

    req = urllib.request.Request(
        webhook_url,
        data=markdown.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/markdown; charset=utf-8",
            "X-Theodosia-Project": project,
            "X-Theodosia-App-Id": app_id,
        },
    )
    try:
        # Scheme validated above; nosec is safe here.
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            err_console.print(
                Text.assemble(
                    ("posted to ", "muted"),
                    (webhook_url, "ok"),
                    (f" (HTTP {resp.status})", "muted"),
                )
            )
    except Exception as exc:
        err_console.print(
            Text.assemble(
                ("webhook POST failed: ", "err"),
                (f"{type(exc).__name__}: {exc}", "err"),
            )
        )
        raise typer.Exit(code=2) from exc
