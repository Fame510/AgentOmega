"""Property + unit tests for the vendored SHACKLE decide() core (P1-P9)."""
from hypothesis import given, strategies as st

from app.shackle import (
    GuardConfig, SessionState, ToolCall, Verdict, HitlMode,
    decide, apply_allow, apply_post_exec, hash_params,
)


def _call(name="tool", params=None, cost=0.0, nonce=1, raw=""):
    return ToolCall(tool_name=name, tool_params_hash=hash_params(params or {"a": 1}),
                    estimated_cost_usd=cost, nonce=nonce, tool_params_raw=raw)


def test_p6_fresh_state_allows():
    st_ = SessionState(budget_initial_usd=1.0, budget_remaining_usd=1.0)
    cfg = GuardConfig(budget_usd=1.0, max_repeat_calls=3)
    assert decide(st_, _call(), cfg).verdict == Verdict.ALLOW


def test_p3_once_tripped_always_tripped():
    st_ = SessionState(circuit_tripped=True, circuit_trip_reason="x")
    cfg = GuardConfig(budget_usd=1.0)
    assert decide(st_, _call(nonce=99), cfg).verdict == Verdict.DENY


def test_p9_duplicate_nonce_denies():
    st_ = SessionState(budget_initial_usd=1.0, budget_remaining_usd=1.0, seen_nonces={7})
    cfg = GuardConfig(budget_usd=1.0)
    assert decide(st_, _call(nonce=7), cfg).verdict == Verdict.DENY


def test_budget_exhausted_denies():
    st_ = SessionState(budget_initial_usd=1.0, budget_remaining_usd=0.0)
    cfg = GuardConfig(budget_usd=1.0)
    assert decide(st_, _call(nonce=3), cfg).verdict == Verdict.DENY


def test_repeat_limit_denies():
    cfg = GuardConfig(max_repeat_calls=2)
    st_ = SessionState()
    c = _call(name="t", nonce=1)
    for i in range(2):
        d = decide(st_, ToolCall("t", c.tool_params_hash, nonce=i + 1), cfg)
        assert d.verdict == Verdict.ALLOW
        apply_allow(st_, ToolCall("t", c.tool_params_hash, nonce=i + 1))
    d = decide(st_, ToolCall("t", c.tool_params_hash, nonce=99), cfg)
    assert d.verdict == Verdict.DENY


def test_hitl_always():
    st_ = SessionState(budget_initial_usd=1.0, budget_remaining_usd=1.0)
    cfg = GuardConfig(budget_usd=1.0, hitl_mode=HitlMode.ALWAYS)
    assert decide(st_, _call(nonce=5), cfg).verdict == Verdict.HITL


def test_p7_deterministic():
    st_ = SessionState(budget_initial_usd=1.0, budget_remaining_usd=1.0)
    cfg = GuardConfig(budget_usd=1.0, max_repeat_calls=3)
    c = _call(nonce=11)
    assert decide(st_, c, cfg).verdict == decide(st_, c, cfg).verdict


@given(cost=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
def test_p1_budget_monotonic_non_increasing(cost):
    st_ = SessionState(budget_initial_usd=50.0, budget_remaining_usd=50.0)
    before = st_.budget_remaining_usd
    apply_post_exec(st_, cost)
    assert st_.budget_remaining_usd <= before
    assert st_.budget_remaining_usd >= 0.0  # P4: never negative
