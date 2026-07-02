# AgentOmega: System Architecture & Design Specification

This document details the concrete integration boundary, message schemas, and verification invariants of AgentOmega (Ω).

## 1. The Core Invariants

AgentOmega maintains 9 core mathematical invariants, verified via property-based testing, to protect against runaway execution loops and unadmitted memory leaks:

| Invariant | Name | Formulation / Definition |
|---|---|---|
| **I1** | **Monotonic Cost Depletion** | B_t = B_t - C_t where C_t >= 0. Budget never increases during a run. |
| **I2** | **Pre-Execution Gating** | For any proposed action a, if Cost(a) > B_t, execution is blocked. |
| **I3** | **Repetition Upper Bound** | Count(a_i) <= N_max. Prevents infinite loop spirals (e.g. Claude Code disk-filling bug). |
| **I4** | **Cascade Failure Trip** | If consecutive error count E >= E_max, transition circuit state to TRIPPED. |
| **I5** | **Cryptographic Audit Chain** | Each log entry L_t is chained: Sign_Ed25519(L_t || H(L_t-1)). |
| **I6** | **Admitted Memory Boundary** | Only sources explicitly registered in the SemeAI memory store can support PROCEED decisions. |
| **I7** | **Zero-Refactor Transparency** | Decorator interception does not modify underlying tool signature or return value shapes. |
| **I8** | **Silence Trace Preservation** | When a SILENCE state triggers, an audit receipt is written to disk detailing the blocked output. |
| **I9** | **Deterministic Fallback** | When budget is depleted, the state-preserving checkpoint is written before execution context is wiped. |

---

## 2. Proof of Resonance (PoR) Workflow

PoR manages the boundary around memory-backed outputs. It handles generation coherence and separates raw archive evidence from admitted memory.

### Decision Matrix

```
+------------------+-----------------------+------------------------------------------+
| State Decision   | Action Taken          | Provenance / Receipt Behavior            |
+------------------+-----------------------+------------------------------------------+
| PROCEED          | Sent to SHACKLE gate  | Dual receipt generated (source + execution)|
| NEEDS_REVIEW     | Human-in-the-loop     | Intercept and await manual validation     |
| SILENCE          | Blocked & Withheld    | Audit preserved, execution denied        |
+------------------+-----------------------+------------------------------------------+
```

### Provenance Trackers
1. **Source Lineage:** Maps each candidate output to its supporting local memory or workspace objects.
2. **Evidence Classification:** Segregates high-entropy archive documents from active admitted memory models.

---

## 3. Wire Formats & Schemas

### PoR Release Receipt

```json
{
  "receipt_id": "por_rcpt_9f1a23e4",
  "timestamp": 1782317305,
  "resonance_score": 0.942,
  "decision": "PROCEED",
  "provenance": {
    "source_id": "mem_obj_aeon_08",
    "source_type": "admitted_memory",
    "lineage_hash": "2a98c054b026df6ae413a"
  },
  "release_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

### SHACKLE AuditLogEntry

```json
{
  "log_id": "shk_log_8829ac1b",
  "previous_log_hash": "19ef2e35059fbe64...",
  "timestamp": 1782317306,
  "budget_remaining": 0.75,
  "action_signature": "execute_browser_action(url='https://github.com')",
  "status": "ALLOWED",
  "signature": "ed25519_sig_abc123..."
}
```
