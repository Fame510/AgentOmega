"""
SHACKLE policy core fused into AgentOmega.

Pure decision function (SP/1.0) + hash-chained audit ledger.
Ported from Fame510/SHACKLE v2/spec/decide.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


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
    max_total_calls: int = 0
    hitl_mode: HitlMode = HitlMode.NEVER
    hitl_budget_threshold: float = 0.0

    def __post_init__(self):
        assert self.budget_usd >= 0
        assert self.max_repeat_calls >= 0
        assert 0.0 <= self.hitl_budget_threshold <= 1.0


@dataclass
class SessionState:
    session_id: str = ""
    agent_id: str = ""
    circuit_tripped: bool = False
    circuit_trip_reason: str = ""
    budget_initial_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    budget_spent_usd: float = 0.0
    total_calls: int = 0
    repeat_counts: Dict[str, int] = field(default_factory=dict)
    last_tool_name: str = ""
    last_tool_params_hash: bytes = b""
    seen_nonces: Set[int] = field(default_factory=set)


@dataclass
class ToolCall:
    tool_name: str
    tool_params_hash: bytes
    estimated_cost_usd: float = 0.0
    nonce: int = 0
    tool_params_raw: str = ""


@dataclass
class Decision:
    verdict: Verdict
    deny_reason: DenyReason = DenyReason.UNSPECIFIED
    human_readable: str = ""


_ERROR_SIGNALS = (
    "401", "unauthorized", "403", "forbidden", "500",
    "internal server error", "502", "bad gateway", "503",
    "service unavailable", "504", "gateway timeout", "timeout",
    "connection refused", "connection reset", "permission denied",
    "access denied", "rate limit", "quota exceeded", "invalid api key",
    "authentication failed", "token expired", "resource exhausted",
)


def has_error_signal(params_raw: str) -> bool:
    if not params_raw:
        return False
    lower = params_raw.lower()
    return any(sig in lower for sig in _ERROR_SIGNALS)


def decide(state: SessionState, call: ToolCall, config: GuardConfig) -> Decision:
    """Core policy decision. Pure function. Zero I/O."""
    if state.circuit_tripped:
        return Decision(Verdict.DENY, DenyReason.CIRCUIT_OPEN,
                        f"Circuit open: {state.circuit_trip_reason}")

    if call.nonce in state.seen_nonces:
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "Duplicate nonce — replay suspected")

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

    if config.max_total_calls > 0 and state.total_calls >= config.max_total_calls:
        return Decision(Verdict.DENY, DenyReason.GLOBAL_LIMIT,
                        f"Global limit: {state.total_calls}/{config.max_total_calls}")

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


# ══════════════════════════════════════════
# Hash-Chained Audit Ledger
# ══════════════════════════════════════════

class AuditLedger:
    """Append-only JSONL ledger; each entry hashes the previous entry (I5)."""

    GENESIS = "0" * 64

    def __init__(self, path: str = "./data/audit.jsonl"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._last_hash = self._recover_last_hash()

    def _recover_last_hash(self) -> str:
        if not os.path.exists(self.path):
            return self.GENESIS
        last = None
        with open(self.path) as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return self.GENESIS
        return json.loads(last)["entry_hash"]

    @staticmethod
    def _hash_entry(entry: dict, prev_hash: str) -> str:
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256((canonical + prev_hash).encode()).hexdigest()

    def append(self, record: dict) -> dict:
        entry = {"ts": time.time(), **record, "prev_hash": self._last_hash}
        entry["entry_hash"] = self._hash_entry(
            {k: v for k, v in entry.items() if k != "entry_hash"}, self._last_hash)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self._last_hash = entry["entry_hash"]
        return entry

    def read_all(self, limit: int = 500) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        return lines[-limit:]

    def verify_chain(self) -> dict:
        entries = self.read_all(limit=1_000_000)
        prev = self.GENESIS
        for i, e in enumerate(entries):
            if e.get("prev_hash") != prev:
                return {"valid": False, "broken_at": i, "total": len(entries)}
            recomputed = self._hash_entry(
                {k: v for k, v in e.items() if k != "entry_hash"}, prev)
            if recomputed != e.get("entry_hash"):
                return {"valid": False, "broken_at": i, "total": len(entries)}
            prev = e["entry_hash"]
        return {"valid": True, "broken_at": None, "total": len(entries)}


ledger = AuditLedger()
