# Hunt Two-Module Rebuild — implementation status

> Canon: **Module 1 = Deep** · **Module 2 = Scanner** · Plan: `abstract-chasing-cerf.md`

## Summary (2026-06-22)

**Plan complete.** All phases landed; runtime-soak (OOS factor promotion, god-object splits) accrues via live watch only.

## Phase completion

| Phase | Status |
|-------|--------|
| 0–1 Shared + lint | **done** |
| 2 Scanner consolidation | **done** |
| 2A Feature facts | **done** — 11 production + 5 quarantine factors |
| 2B Indicator prune | **done** |
| 3 Deep independence | **done** |
| 4 Legacy eradication | **done** |
| 5 Telegram macquettes | **done** |
| 6 Canonical numbering | **done** |
| 7 Docs overhaul | **done** |
| 8 Calibration + OOS | **done** |
| 9 Remaining debt | **done** (docs + LOC) — god-object splits optional maintenance while budget green |

## P0 / P0'

All items **done**, including:
- Cross-module arbiter (`shared/delivery/cross_module.py`)
- Max RR 10R in contract
- Deep signal queue TTL 2.5h (`signal_queue_ttl_hours`)
- Expansion lab body moved to `_dev/expansion_lab/` (shims at `analysis/expansion_engine/`)

## Fusion factors

**Production (11):** `book`, `structure`, `funding`, `flow`, `oi_pressure`, `compression`, `oi_acceleration`, `funding_velocity`, `poc_migration`, `va_contraction`, `liquidity_void_path`

**Quarantine (5):** `market_maker_trap`, `whale_activity`, `cross_exchange_divergence`, `cross_funding_consensus`, `spot_futures_pressure`

## Verification

```bash
bash hunt/scripts/verify_hunt_rebuild.sh
```

## Live soak (ongoing, not blocking)

- Ledger accrual → OOS promotion of quarantine factors (`need_n=172`)
- God-object splits when LOC pressure returns
