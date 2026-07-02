# AgentOmega (Ω)

> **\"Generation is not release authority.\"**

AgentOmega represents the \"nuclear fusion\" of **Aeon_Dux's** browser runtime and **SHACKLE's** mathematically verified, pre-execution safety circuit breaker. By binding local-first Proof of Resonance (PoR) semantics with framework-agnostic execution guards, AgentOmega establishes a complete, zero-refactor sovereignty layer for autonomous AI agents.

---

## The Fusion Topology

```
                  [ Agent/Generator (Aeon_Dux) ]
                                |
                                | proposes action / output
                                v
                [ SemeAI / Proof of Resonance (PoR) ]
                 - Checks local memory alignment
                 - Resolves PROCEED / NEEDS_REVIEW / SILENCE
                                |
                                | if PROCEED
                                v
                    [ SHACKLE Execution Guard ]
                     - Depletes dollar budget monotonically
                     - Tracks call repetitions & cascade errors
                     - Signs audit logs via Ed25519
                                |
                                | if ALLOWED
                                v
                       [ External Systems / Tools ]
```

### 1. Aeon_Dux Browser Runtime
Provides high-performance, local-first browser navigation and generation coherence. It serves as the primary action-generator within the AgentOmega environment.

### 2. SHACKLE Pre-Execution Circuit Breaker
Enforces mathematical invariants at the pre-execution boundary.
- **Pre-execution Interception:** Decides whether proposed actions are safe to execute *before* they are sent to external tools or APIs.
- **Monotonic Cost Depletion:** Hard-budget tracking where the budget monotonically decreases to prevent silent runway loops.
- **Error Signal Cascade:** Captures and handles tool execution failures (401/403/500/timeouts) to break loop spirals immediately.
- **Ed25519 Auditing:** Signs every audit log entry to establish cryptographic execution provenance.

### 3. Proof of Resonance (PoR) Release Semantics
Implements local-governed memory and release-provenance protocol to protect the boundary of memory-backed outputs:
- **PROCEED**: Candidate meets resonance threshold; approved for tool/execution gate.
- **NEEDS_REVIEW**: Escalates to human-in-the-loop validation.
- **SILENCE**: Candidate generated, release denied, audit preserved. Withholds outputs to prevent leakage of unadmitted memory or sensitive workspace data.

---

## Mathematical Invariants (SHACKLE Core)

AgentOmega enforces **9 strict mathematical invariants** at the execution boundary:
1. **Monotonic Depletion:** Current Budget B_t is strictly non-increasing over time (B_t <= B_t-1).
2. **Pre-Execution Halt:** If proposed tool cost C_t > B_t, halt immediately with a BudgetExceeded error without executing the tool.
3. **Bounded Repetition:** A single tool signature cannot exceed N_max repeat calls without state-progression signals.
4. **Cascade Escalation:** An error signal cascade triggers circuit breaker trip on E_max consecutive failures.
5. **Cryptographic Integrity:** Every execution state transition generates an Audit Log signed with an Ed25519 private key.
*(Complete list of invariants detailed in ARCHITECTURE.md)*

---

## Quick Start

```python
from shackle import Guard
from agent_omega import OmegaRuntime

# Initialize the runtime with Aeon_Dux and SHACKLE
runtime = OmegaRuntime(
    runtime_engine=\"aeon_dux\",
    safety_policy=\"shackle_sp_1\"
)

# Apply the guard to protect tool boundaries
@Guard(budget=1.00, max_repeat_calls=3, timeout_seconds=300)
def web_navigation_and_action(url, action):
    return runtime.execute_browser_action(url, action)
```

---

## Collaboration & Licensing
AgentOmega is built for sovereign, production-grade AI agency.
- **SHACKLE Core:** AGPLv3 License.
- **SemeAI / PoR Integration:** Joint design-partner specification (co-authored by Dante Bullock).
