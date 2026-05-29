"""``_as_transport`` defaults log_file to sys.__stderr__.

Regression for a dogfood finding: FastMCP wraps ``sys.stderr`` in a
``StringIO`` inside a running server for protocol cleanliness, and the
upstream subprocess opener (``mcp.client.stdio``) calls ``.fileno()`` on
whatever stderr it inherits. The default ``log_file=sys.__stderr__``
makes ``mount(upstream={"server": {"command": ..., "args": [...]}})``
work from inside a running FastMCP server. Without the default, the
subprocess crashes with ``io.UnsupportedOperation: fileno`` and the
documented happy-path config is dead on arrival.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from theodosia.upstream import _as_transport


def test_default_log_file_is_real_stderr():
    """The transport carries ``sys.__stderr__`` so the subprocess inherits a real fd."""
    t = _as_transport({"command": "echo", "args": ["hi"]})
    assert t.log_file is sys.__stderr__


def test_explicit_log_file_path_wins():
    p = Path("/tmp/upstream.log")
    t = _as_transport({"command": "echo", "args": ["hi"], "log_file": p})
    assert t.log_file == p


def test_explicit_log_file_textio_wins():
    buf = io.StringIO()
    t = _as_transport({"command": "echo", "args": ["hi"], "log_file": buf})
    assert t.log_file is buf


def test_default_survives_wrapped_sys_stderr(monkeypatch):
    """Simulate FastMCP wrapping ``sys.stderr`` in a StringIO.

    The wrap targets ``sys.stderr`` only; ``sys.__stderr__`` is the original
    interpreter-level stderr and is unaffected. The transport must point at
    the unwrapped one or subprocess Popen breaks on ``stderr.fileno()``.
    """
    fake_wrap = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_wrap)
    t = _as_transport({"command": "echo", "args": ["hi"]})
    assert t.log_file is not fake_wrap
    assert t.log_file is sys.__stderr__
