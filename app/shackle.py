"""
SHACKLE governor for AgentOmega
===============================
Fuses the canonical SHACKLE SP/1.0 decision core with an embedded,
self-contained, Ed25519-signed, hash-chained audit ledger.

Vendors the canonical pure decision function (decide()) from SHACKLE
v2/spec/decide.py -- 8 stacked layers, zero I/O, deterministic (properties
P1-P9) -- and pairs it with an append-only audit ledger that runs anywhere
(no Postgres required). The contract is the SHACKLE source code; no
"proof-of-receipt" workflow exists or is implied.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

try:
    from nacl.signing import SigningKey, VerifyKey
    from nacl.encoding import HexEncoder
    _HAVE_NACL = True
except Exception:  # pragma: no cover
    _HAVE_NACL = False


class Verdict(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HITL = "HITL"


class DenyReason(Enum):
    UNSPECIFIED = "unspecified"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_REPEAT_EXCEEDED = "max_repeat_exceeded"
    CIRCUIT_OPEN = "circuit_open"
    WINDOW_EXCEEDED = "window_exceeded"
    GLOBAL_LIMIT = "global_limit"
    POLICY_VIOLATION = "policy_violation"
    AUTH_FAILED = "auth_failed"


class HitlMode(Enum):
    NEVER = "never"
    ON_DENY = "on_deny"
    ON_THRESHOLD = "on_threshold"
    ALWAYS = "always"


@dataclass
class GuardConfig:
    budget_usd: float = 0.0
    max_repeat_calls: int = 0
    error_amplification: bool = True
    timeout_seconds: int = 0
    window_duration_s: int = 0
    window_max_calls: int = 0
    max_total_calls: int = 0
    probabilistic_deny: bool = False
    deny_jitter_ratio: float = 0.0
    hitl_mode: HitlMode = HitlMode.NEVER
    hitl_budget_threshold: float = 0.0
    parent_guard_id: str = ""

    def __post_init__(self):
        assert self.budget_usd >= 0, "budget_usd must be >= 0"
        assert self.max_repeat_calls >= 0, "max_repeat_calls must be >= 0"
        assert 0.0 <= self.deny_jitter_ratio <= 1.0
        assert 0.0 <= self.hitl_budget_threshold <= 1.0


@dataclass
class SessionState:
    session_id: str = ""
    agent_id: str = ""
    organization_id: str = ""
    circuit_tripped: bool = False
    circuit_trip_reason: str = ""
    budget_initial_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    budget_spent_usd: float = 0.0
    total_calls: int = 0
    repeat_counts: Dict[str, int] = field(default_factory=dict)
    window_counts: Dict[str, int] = field(default_factory=dict)
    last_tool_name: str = ""
    last_tool_params_hash: bytes = b""
    seen_nonces: Set[int] = field(default_factory=set)


@dataclass
class ToolCall:
    tool_name: str
    tool_params_hash: bytes
    estimated_cost_usd: float = 0.0
    nonce: int = 0
    parent_guard_id: str = ""
    tool_params_raw: str = ""


@dataclass
class Decision:
    verdict: Verdict
    deny_reason: DenyReason = DenyReason.UNSPECIFIED
    human_readable: str = ""
    probabilistic_deny: bool = False


_ERROR_SIGNALS = (
    "401", "unauthorized", "403", "forbidden", "500",
    "internal server error", "502", "bad gateway", "503",
    "service unavailable", "504", "gateway timeout", "timeout",
    "connection refused", "connection reset", "no route to host",
    "permission denied", "access denied", "rate limit",
    "quota exceeded", "invalid api key", "authentication failed",
    "token expired", "model not found", "resource exhausted",
    "deadline exceeded",
)


def has_error_signal(params_raw: str) -> bool:
    if not params_raw:
        return False
    lower = params_raw.lower()
    return any(sig in lower for sig in _ERROR_SIGNALS)


def decide(state: SessionState, call: ToolCall, config: GuardConfig,
           rng_float: float = 0.0) -> Decision:
    """Core policy decision. 8 stacked layers. Pure function. Zero I/O."""
    if state.circuit_tripped:
        return Decision(Verdict.DENY, DenyReason.CIRCUIT_OPEN,
                        f"Circuit open: {state.circuit_trip_reason}")

    if call.nonce in state.seen_nonces:
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "Duplicate nonce - replay attack suspected")

    if config.budget_usd > 0:
        if state.budget_remaining_usd <= 0:
            return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                            f"Budget exhausted: ${state.budget_spent_usd:.4f} / ${state.budget_initial_usd:.4f}")
        if config.hitl_mode == HitlMode.ON_THRESHOLD and state.budget_initial_usd > 0:
            fraction = state.budget_remaining_usd / state.budget_initial_usd
            if fraction <= config.hitl_budget_threshold:
                return Decision(Verdict.HITL,
                                human_readable=f"Budget threshold: {fraction:.1%} remaining")
        if call.estimated_cost_usd > state.budget_remaining_usd:
            if config.hitl_mode in (HitlMode.ON_DENY, HitlMode.ALWAYS):
                return Decision(Verdict.HITL,
                                human_readable=f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")
            return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                            f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")

    if config.max_repeat_calls > 0:
        is_repeat = (call.tool_name == state.last_tool_name and
                     call.tool_params_hash == state.last_tool_params_hash)
        if is_repeat:
            repeat_count = state.repeat_counts.get(call.tool_name, 0)
            limit = config.max_repeat_calls
            if config.error_amplification and has_error_signal(call.tool_params_raw):
                limit = max(1, config.max_repeat_calls - 1)
            if repeat_count >= limit:
                return Decision(Verdict.DENY, DenyReason.MAX_REPEAT_EXCEEDED,
                                f"'{call.tool_name}' repeated {repeat_count + 1}x (limit: {config.max_repeat_calls})")

    if config.window_max_calls > 0:
        count = state.window_counts.get(call.tool_name, 0)
        if count >= config.window_max_calls:
            return Decision(Verdict.DENY, DenyReason.WINDOW_EXCEEDED,
                            f"'{call.tool_name}' {count}x in {config.window_duration_s}s window (limit: {config.window_max_calls})")

    if config.max_total_calls > 0 and state.total_calls >= config.max_total_calls:
        return Decision(Verdict.DENY, DenyReason.GLOBAL_LIMIT,
                        f"Global limit: {state.total_calls}/{config.max_total_calls}")

    if config.probabilistic_deny and config.budget_usd > 0 and state.budget_initial_usd > 0:
        ratio = state.budget_remaining_usd / state.budget_initial_usd
        if ratio < 0.2:
            prob = config.deny_jitter_ratio * (1.0 - ratio)
            if rng_float < prob:
                return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                                "Budget enforcement (probabilistic)", probabilistic_deny=True)

    if config.hitl_mode == HitlMode.ALWAYS:
        return Decision(Verdict.HITL, human_readable="HITL required for all calls")

    return Decision(Verdict.ALLOW, human_readable="Within all guard thresholds")


def apply_allow(state: SessionState, call: ToolCall) -> None:
    state.total_calls += 1
    state.seen_nonces.add(call.nonce)
    is_repeat = (call.tool_name == state.last_tool_name and
                 call.tool_params_hash == state.last_tool_params_hash)
    if is_repeat:
        state.repeat_counts[call.tool_name] = state.repeat_counts.get(call.tool_name, 0) + 1
    else:
        state.repeat_counts[call.tool_name] = 1
    state.window_counts[call.tool_name] = state.window_counts.get(call.tool_name, 0) + 1
    state.last_tool_name = call.tool_name
    state.last_tool_params_hash = call.tool_params_hash


def apply_deny(state: SessionState, reason: str) -> None:
    state.circuit_tripped = True
    state.circuit_trip_reason = reason


def apply_post_exec(state: SessionState, actual_cost_usd: float) -> None:
    if actual_cost_usd <= 0:
        return
    state.budget_spent_usd += actual_cost_usd
    state.budget_remaining_usd = max(0.0, state.budget_initial_usd - state.budget_spent_usd)


def hash_params(params: dict) -> bytes:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()


_GENESIS = "0" * 64


class AuditLedger:
    """Append-only, Ed25519-signed, SHA-256 hash-chained, file-backed ledger.

    Writes are serialized by an asyncio.Lock and performed off the event loop
    via asyncio.to_thread so the async gate never blocks on disk I/O.
    verify_chain() recomputes the chain and validates every signature.
    """

    def __init__(self, path: str, signing_key_hex: Optional[str] = None):
        self.path = path
        self._lock = asyncio.Lock()
        self._last_hash = _GENESIS
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.signing_key = None
        self.verify_key_hex = ""
        if _HAVE_NACL:
            if signing_key_hex:
                self.signing_key = SigningKey(signing_key_hex, encoder=HexEncoder)
            else:
                self.signing_key = SigningKey.generate()
            self.verify_key_hex = self.signing_key.verify_key.encode(encoder=HexEncoder).decode()
        self._recover_last_hash()

    def _recover_last_hash(self) -> None:
        if not os.path.exists(self.path):
            return
        last = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if last:
            try:
                self._last_hash = json.loads(last)["record_hash"]
            except Exception:
                pass

    @staticmethod
    def _canonical(obj: dict) -> str:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    def _build_record(self, event: dict) -> dict:
        core = dict(event)
        core["prev_hash"] = self._last_hash
        record_hash = hashlib.sha256(
            (self._canonical(core) + self._last_hash).encode()).hexdigest()
        core["record_hash"] = record_hash
        if self.signing_key is not None:
            core["signature"] = self.signing_key.sign(record_hash.encode()).signature.hex()
            core["verify_key"] = self.verify_key_hex
        else:
            core["signature"] = ""
            core["verify_key"] = ""
        return core

    def _append_sync(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    async def append(self, event: dict) -> dict:
        async with self._lock:
            record = self._build_record(event)
            await asyncio.to_thread(self._append_sync, record)
            self._last_hash = record["record_hash"]
            return record

    async def log_decision(self, session_id: str, tool_name: str, verdict: str,
                           reason: str = "", metadata: Optional[dict] = None) -> dict:
        return await self.append({
            "ts": time.time(), "event_type": "decision", "session_id": session_id,
            "tool_name": tool_name, "verdict": verdict, "reason": reason,
            "metadata": metadata or {},
        })

    async def log_execution(self, session_id: str, tool_name: str, ok: bool,
                            cost_usd: float = 0.0, error: str = "",
                            metadata: Optional[dict] = None) -> dict:
        return await self.append({
            "ts": time.time(), "event_type": "execution", "session_id": session_id,
            "tool_name": tool_name, "ok": ok, "cost_usd": cost_usd, "error": error,
            "metadata": metadata or {},
        })

    def read_all(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> dict:
        records = self.read_all()
        prev = _GENESIS
        for i, rec in enumerate(records):
            core = {k: v for k, v in rec.items()
                    if k not in ("record_hash", "signature", "verify_key")}
            if core.get("prev_hash") != prev:
                return {"valid": False, "count": len(records), "broken_at": i,
                        "reason": "prev_hash mismatch"}
            expected = hashlib.sha256((self._canonical(core) + prev).encode()).hexdigest()
            if expected != rec.get("record_hash"):
                return {"valid": False, "count": len(records), "broken_at": i,
                        "reason": "record_hash mismatch (tampered content)"}
            sig, vk = rec.get("signature"), rec.get("verify_key")
            if sig and vk and _HAVE_NACL:
                try:
                    VerifyKey(vk, encoder=HexEncoder).verify(
                        rec["record_hash"].encode(), bytes.fromhex(sig))
                except Exception:
                    return {"valid": False, "count": len(records), "broken_at": i,
                            "reason": "signature verification failed"}
            prev = rec["record_hash"]
        return {"valid": True, "count": len(records), "broken_at": None, "reason": "ok"}


class ShackleGovernor:
    """Per-session governor: binds config + state + ledger, exposes gate()."""

    def __init__(self, session_id: str, config: GuardConfig, ledger: AuditLedger,
                 agent_id: str = "agentomega"):
        self.session_id = session_id
        self.config = config
        self.ledger = ledger
        self.state = SessionState(
            session_id=session_id, agent_id=agent_id,
            budget_initial_usd=config.budget_usd,
            budget_remaining_usd=config.budget_usd)
        self._nonce = 0
        self._pending_call: Optional[ToolCall] = None

    def next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    async def gate(self, tool_name: str, params: dict,
                   estimated_cost_usd: float = 0.0, rng_float: float = 0.0) -> Decision:
        call = ToolCall(
            tool_name=tool_name, tool_params_hash=hash_params(params),
            estimated_cost_usd=estimated_cost_usd, nonce=self.next_nonce(),
            tool_params_raw=json.dumps(params, sort_keys=True)[:4000])
        decision = decide(self.state, call, self.config, rng_float)
        await self.ledger.log_decision(
            self.session_id, tool_name, decision.verdict.value,
            decision.human_readable, {"deny_reason": decision.deny_reason.value})
        self._pending_call = call
        return decision

    def commit_allow(self) -> None:
        if self._pending_call is not None:
            apply_allow(self.state, self._pending_call)

    def trip(self, reason: str) -> None:
        apply_deny(self.state, reason)

    def account(self, actual_cost_usd: float) -> None:
        apply_post_exec(self.state, actual_cost_usd)

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "circuit_tripped": self.state.circuit_tripped,
            "circuit_trip_reason": self.state.circuit_trip_reason,
            "total_calls": self.state.total_calls,
            "budget_initial_usd": self.state.budget_initial_usd,
            "budget_remaining_usd": self.state.budget_remaining_usd,
            "budget_spent_usd": self.state.budget_spent_usd,
        }
