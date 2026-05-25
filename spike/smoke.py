"""Run both spikes end to end:  uv run --no-sync python spike/smoke.py"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from appfile import load_app, serve_appfile  # noqa: E402
from ledger import HashChainedLedger, verify_ledger  # noqa: E402


def test_ledger() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        led = HashChainedLedger(path)
        led.append({"action": "page_oncall", "outcome": "allowed"})
        led.append({"action": "close_incident", "outcome": "refused", "reason": "not reachable"})
        led.append({"action": "verify", "outcome": "allowed"})

        ok, problems = verify_ledger(path)
        assert ok, f"clean ledger should verify, got {problems}"
        print("ledger: clean chain verifies OK")

        # tamper: rewrite an earlier entry's content, keep its hash
        lines = path.read_text().splitlines()
        lines[1] = lines[1].replace('"refused"', '"allowed"')  # forge the refusal away
        path.write_text("\n".join(lines) + "\n")

        ok, problems = verify_ledger(path)
        assert not ok, "tampered ledger must fail verification"
        print(f"ledger: tamper detected -> {problems}")


def test_appfile() -> None:
    appfile = Path(__file__).parent / "incident.app"
    build, meta = load_app(appfile)
    assert meta.get("name") == "incident", meta
    app = build()
    actions = {a.name for a in app.graph.actions}
    assert {"acknowledge", "verify", "resolve"} <= actions, actions
    print(f"appfile: loaded '{meta['name']}' with actions {sorted(actions)}")

    server = serve_appfile(appfile)
    tools = {t.name for t in server._tool_manager._tools.values()} if hasattr(server, "_tool_manager") else set()
    print(f"appfile: mounted as MCP server '{server.name}' (tools surface present: {bool(tools) or 'n/a'})")


if __name__ == "__main__":
    test_ledger()
    test_appfile()
    print("\nBOTH SPIKES OK")
