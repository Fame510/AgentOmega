# AgentOmega (Ω)

> "Generation is not release authority."

AgentOmega represents the fusion of **Aeon_Dux's browser runtime** and **SHACKLE's mathematically verified safety circuit breaker** for autonomous AI agents.

## Core Components
- **Aeon_Dux Runtime**: High-performance DOM/API interaction engine
- **SHACKLE Guard**: Enforces pre-execution policy checks (budget, repetition limits, cryptographic audits)

## Mathematical Invariants (SHACKLE Core)
1. Monotonic budget depletion
2. Bounded repetition limits  
3. Cryptographic execution provenance

## The Fusion

Every browser action proposed by the Aeon_Dux runtime passes through the SHACKLE
`decide()` gate **before** execution. Verdicts:

- **ALLOW** — action runs; budget depleted, state updated
- **DENY** — circuit breaker trips; run halts permanently
- **HITL** — execution pauses; the operator approves / skips / aborts from the web UI

Every verdict is appended to a hash-chained audit ledger (`data/audit.jsonl`)
that can be cryptographically verified from the Govern tab.

## Web Console

Three tabs, one app:

1. **⚒ Build** — create agents: name, goal template, start URL, and a SHACKLE
   policy (budget, repeat limit, total call limit, timeout, HITL mode).
2. **▶ Run** — pick an agent, launch a goal, watch live telemetry with a real-time
   budget bar, call counter, and circuit state. HITL prompts appear as a modal.
3. **⚖ Govern** — inspect the hash-chained audit ledger, verify chain integrity,
   and kill runaway sessions from the fleet panel.

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app.server:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

Authored and maintained by [Fame510](https://github.com/Fame510)