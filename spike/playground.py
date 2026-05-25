"""Both spikes, together, end to end:
  - load incident.app and mount it as an MCP server
  - drive it over the real `step` tool through a FastMCP client, including a
    refusal (resolve before verify)
  - log every attempt into the hash-chained ledger
  - verify the ledger, then tamper it and watch verification catch the line

  uv run --no-sync python spike/playground.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from appfile import load_app, mount_kwargs  # noqa: E402
from ledger import HashChainedLedger, verify_ledger  # noqa: E402
from fastmcp import Client  # noqa: E402
from theodosia import mount  # noqa: E402


async def main() -> None:
    target, meta, ns = load_app(Path(__file__).parent / "incident.app")
    kwargs = mount_kwargs(meta, ns)
    print("incident.app carried this MCP-server config (the part Burr has no need for):")
    for k, v in kwargs.items():
        shown = v if not isinstance(v, dict) else {kk: "..." for kk in v}
        print(f"  {k} = {shown}")
    server = mount(target, **kwargs)
    print(f"\nmounted  ->  MCP server '{server.name}'\n")

    tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
    led = HashChainedLedger(tmp)

    # An agent that tries to close early, then walks it properly.
    plan = ["resolve", "acknowledge", "verify", "resolve"]

    async with Client(server) as client:
        for action in plan:
            res = await client.call_tool("step", {"action": action, "inputs": {}})
            out = res.structured_content
            if "error" in out:
                mark, detail = "REFUSED", out["error"]
                led.append({"action": action, "outcome": "refused", "reason": out["error"]})
            else:
                mark, detail = "ok", "-> " + ", ".join(out.get("valid_next_actions") or ["(terminal)"])
                led.append({"action": action, "outcome": "allowed",
                            "valid_next": out.get("valid_next_actions")})
            print(f"  step {action:<12} {mark:<8} {detail}")

    print(f"\nledger written to {tmp.name} ({sum(1 for _ in tmp.open())} entries)")
    ok, problems = verify_ledger(tmp)
    print(f"verify (clean): {'PASS' if ok else 'FAIL ' + str(problems)}")

    # Now forge the audit: pretend the early close was allowed.
    lines = tmp.read_text().splitlines()
    lines[0] = lines[0].replace('"refused"', '"allowed"')
    tmp.write_text("\n".join(lines) + "\n")
    ok, problems = verify_ledger(tmp)
    print(f"verify (after forging the refusal into 'allowed'): "
          f"{'PASS (!?)' if ok else 'TAMPER CAUGHT -> ' + problems[0]}")


if __name__ == "__main__":
    asyncio.run(main())
