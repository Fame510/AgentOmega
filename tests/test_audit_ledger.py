"""Tests for the append-only, hash-chained, signed audit ledger."""
import asyncio
import json
import os
import tempfile

from app.shackle import AuditLedger


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_chain_valid_after_appends():
    with tempfile.TemporaryDirectory() as d:
        led = AuditLedger(os.path.join(d, "audit.jsonl"))
        _run(led.log_decision("s1", "click", "ALLOW", "ok"))
        _run(led.log_execution("s1", "click", ok=True, cost_usd=0.001))
        _run(led.log_decision("s1", "type", "DENY", "budget"))
        res = led.verify_chain()
        assert res["valid"] is True
        assert res["count"] == 3


def test_tamper_detected():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit.jsonl")
        led = AuditLedger(path)
        _run(led.log_decision("s1", "click", "ALLOW", "ok"))
        _run(led.log_decision("s1", "click", "ALLOW", "ok2"))
        # Tamper: rewrite the first record's reason
        lines = open(path).read().splitlines()
        rec = json.loads(lines[0])
        rec["reason"] = "HACKED"
        lines[0] = json.dumps(rec)
        open(path, "w").write("\n".join(lines) + "\n")
        res = led.verify_chain()
        assert res["valid"] is False
        assert res["broken_at"] == 0
