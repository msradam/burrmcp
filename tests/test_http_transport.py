"""HTTP transport: real subprocess server, two real HTTP clients.

Most tests use FastMCP's in-process ``Client(server)`` which runs the
server in the same Python process. That's fast and exercises the
adapter, but it doesn't exercise the wire format. This test spawns
``examples/http_serve.py`` as a subprocess, opens two HTTP clients
against it, and verifies that the per-session isolation we ship works
on the real wire format people deploy.

The test is slower than the in-process suite (subprocess startup +
HTTP round-trips) but still finishes in a few seconds. CI runs it.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server did not bind to {host}:{port} within {timeout}s")


@pytest.fixture
def http_server():
    """Spawn examples/http_serve.py as a subprocess on a free port."""
    port = _find_free_port()
    env = {
        "BURR_MCP_HOST": "127.0.0.1",
        "BURR_MCP_PORT": str(port),
        "PATH": str(Path(sys.executable).parent),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "examples" / "http_serve.py"),
        ],
        env={**dict(__import__("os").environ), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_two_http_clients_have_independent_state(http_server):
    """Two concurrent HTTP clients on a factory-mounted server see independent state.

    This is the wire-format counterpart to ``test_per_session``. If
    FastMCP's session id ever became per-connection-pool instead of
    per-client, this test would catch it.
    """
    async with Client(http_server) as client_a:
        await client_a.call_tool(
            "step",
            {"action": "take_order", "inputs": {"item": "latte", "qty": 1}},
        )

        async with Client(http_server) as client_b:
            state_b = json.loads((await client_b.read_resource("theodosia://state"))[0].text)
            assert state_b.get("stage") == "new", f"HTTP session B saw session A's state: {state_b}"

            await client_b.call_tool(
                "step",
                {"action": "take_order", "inputs": {"item": "americano", "qty": 3}},
            )
            state_b_after = json.loads((await client_b.read_resource("theodosia://state"))[0].text)
            assert state_b_after["item"] == "americano"

        state_a = json.loads((await client_a.read_resource("theodosia://state"))[0].text)
        assert state_a["item"] == "latte", (
            f"HTTP session A's state was mutated by session B: {state_a}"
        )


@pytest.mark.asyncio
async def test_http_history_is_per_session(http_server):
    """Per-session history works over the wire too."""
    async with Client(http_server) as client_a:
        await client_a.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})

        async with Client(http_server) as client_b:
            await client_b.call_tool(
                "step", {"action": "take_order", "inputs": {"item": "espresso"}}
            )
            history_b = json.loads((await client_b.read_resource("theodosia://history"))[0].text)
            assert len(history_b) == 1
            assert history_b[0]["inputs"]["item"] == "espresso"

        history_a = json.loads((await client_a.read_resource("theodosia://history"))[0].text)
        assert len(history_a) == 1
        assert history_a[0]["inputs"]["item"] == "latte"
