"""Smoke test for ``theodosia primer``."""

from __future__ import annotations

import asyncio

from rich.console import Console

from theodosia.primer import _run


def test_primer_runs_clean():
    console = Console(force_terminal=False, no_color=True, width=120)
    code = asyncio.run(_run(console))
    assert code == 0


def test_primer_cli_entry_succeeds(capsys, monkeypatch):
    from theodosia import cli

    monkeypatch.setattr("sys.argv", ["theodosia", "primer"])
    code = cli.main(["primer"])
    assert code == 0
    out = capsys.readouterr().out
    assert "theodosia primer" in out
    assert "take_order" in out
    assert "REFUSE" in out
    assert "valid_next_actions" in out
