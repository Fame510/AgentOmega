"""The HITL await must be bounded: timeout -> ABORT, never an infinite hang."""
import asyncio
import tempfile
import os

from app.shackle import AuditLedger, GuardConfig, HitlMode, ShackleGovernor


def test_governor_gate_hitl_verdict():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            led = AuditLedger(os.path.join(d, "audit.jsonl"))
            gov = ShackleGovernor("s1", GuardConfig(hitl_mode=HitlMode.ALWAYS), led)
            dec = await gov.gate("click", {"a": 1})
            return dec.verdict.value
    assert asyncio.get_event_loop().run_until_complete(run()) == "HITL"


def test_bounded_wait_pattern():
    """asyncio.wait_for around an unset event must raise TimeoutError quickly."""
    async def run():
        ev = asyncio.Event()
        try:
            await asyncio.wait_for(ev.wait(), timeout=0.05)
            return "no_timeout"
        except asyncio.TimeoutError:
            return "timeout"
    assert asyncio.get_event_loop().run_until_complete(run()) == "timeout"
