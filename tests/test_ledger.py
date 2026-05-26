"""Hash-chained tamper-evident ledger: the chain itself, plus the adapter
writing a verifiable ledger next to a session's tracker log."""

from __future__ import annotations

import json

import pytest
from coffee_order import build_server
from fastmcp import Client

from theodosia import ServingMode
from theodosia.ledger import GENESIS, HashChainedLedger, verify_ledger


def test_append_chains_and_verifies(tmp_path):
    led = HashChainedLedger(tmp_path / "ledger.jsonl")
    first = led.append({"seq": 0, "action": "take_order"})
    second = led.append({"seq": 1, "action": "pay"})
    assert first["prev"] == GENESIS
    assert second["prev"] == first["hash"]
    ok, problems = verify_ledger(tmp_path / "ledger.jsonl")
    assert ok and problems == []


def test_missing_ledger_is_vacuously_valid(tmp_path):
    ok, problems = verify_ledger(tmp_path / "nope.jsonl")
    assert ok and problems == []


def test_altered_entry_is_detected(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0, "action": "take_order", "refused": False})
    led.append({"seq": 1, "action": "pay", "refused": True})

    # Forge the first line: flip the refusal but leave its recorded hash.
    lines = path.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["refused"] = True
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    ok, problems = verify_ledger(path)
    assert not ok
    assert any("line 0" in p and "hash mismatch" in p for p in problems)


def test_deleted_line_breaks_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    led.append({"seq": 2})
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # drop the middle line
    ok, problems = verify_ledger(path)
    assert not ok
    assert any("prev-link mismatch" in p for p in problems)


@pytest.mark.asyncio
async def test_driven_session_writes_verifiable_ledger(tmp_path, monkeypatch):
    # coffee_order wires tracker(project="coffee-order-demo") -> ~/.theodosia;
    # conftest's _isolate_burr_home redirects HOME to tmp_path, so the ledger
    # lands under tmp_path/.theodosia.
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "latte", "qty": 1}}
        )
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        await client.call_tool("step", {"action": "fulfill", "inputs": {}})

    ledgers = list(tmp_path.rglob("ledger.jsonl"))
    assert ledgers, "expected a ledger.jsonl under the isolated tracker home"
    ok, problems = verify_ledger(ledgers[0])
    assert ok, problems
    # Three accepted steps chained.
    assert len(ledgers[0].read_text().splitlines()) >= 3


@pytest.mark.asyncio
async def test_ledger_records_refusals_too(tmp_path):
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        # Refused: pay before take_order.
        await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        await client.call_tool(
            "step", {"action": "take_order", "inputs": {"item": "mocha", "qty": 1}}
        )

    ledgers = list(tmp_path.rglob("ledger.jsonl"))
    assert ledgers
    rows = [json.loads(line) for line in ledgers[0].read_text().splitlines()]
    assert any(r.get("refused") for r in rows), "refusal should be chained into the ledger"
    ok, _ = verify_ledger(ledgers[0])
    assert ok
