# Multi-Instance Deployment for Price Action Trading

> 2026-06-02: Verified that delegate_task subagents CANNOT perform price action analysis (they invent rules). The correct approach is separate Hermes profiles per instrument.

## Architecture

```
~/.hermes/profiles/
├── default/          ← Main profile
├── btc-trader/       ← BTC only
│   ├── skills/price-action-trading/ (shared references/knowledge/)
│   ├── memory/ (BTC-only trading memory)
│   └── logs/ (BTC-only logs)
├── eth-trader/       ← ETH only
├── sol-trader/       ← SOL only
├── xau-trader/       ← XAU only
└── xag-trader/       ← XAG only
```

## What's Shared vs Isolated

| Component | Shared? | Reason |
|-----------|:-------:|--------|
| references/knowledge/ | ✓ | Same Al Brooks rules for all instruments |
| assets/templates/ | ✓ | Same journal/review format |
| assets/examples/ | ✓ | Same quality standard |
| references/ | ✓ | Same architecture docs |
| references/SOUL.md | ✓ | Same execution rules |
| SKILL.md | ✓ | Same entry point |
| memory/ | ✗ | Each instrument has its own trading history |
| logs/ | ✗ | Each instrument has its own log data |

## Hardware Requirements (Verified 2026-06-02)

| Resource | Available | Per Instance | Max Instances |
|----------|-----------|-------------|:-------------:|
| CPU | 12 physical cores | ~1 core | 12 |
| RAM | 4.8 GB free | ~350 MB | 13 |
| Disk | 217.5 GB free | ~50 MB | 4000+ |
| **Recommended** | | | **10** |

## Why Subagents Don't Work

1. **File access**: Subagents run in isolated sandboxes, cannot access Windows filesystem
2. **Rule accuracy**: Even with 18KB context of rules, subagents invent their own SB/CT/BAN definitions
3. **Quality**: Subagent output uses wrong rule IDs, wrong decision tree, wrong everything

## Verification

Test each profile with a single instrument analysis. Compare against `assets/examples/01-btc-20260602.md` quality standard. All 26 Steps must be present with correct workflow.md + strategy_workflow.md references.
