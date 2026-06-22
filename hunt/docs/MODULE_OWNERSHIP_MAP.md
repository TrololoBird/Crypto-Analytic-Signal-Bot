# Module ownership map

Canon: **Module 1 = Deep** (`hunt_core/deep/`) · **Module 2 = Scanner** (`hunt_core/scanner/`) · **Shared** (`hunt_core/shared/`)

| Path | Target zone | Role |
|------|-------------|------|
| `shared/market/` (future) | shared | CCXT plane — currently `market/` top-level |
| `shared/facts/` | shared | Layer-0 row/market accessors |
| `shared/mathlib/` | shared | robust_z, quantile, ols (re-export) |
| `shared/primitives/` | shared | ATR bands, forecast bands, conviction math |
| `shared/ledger/` | shared | Outcome + shadow reject logging |
| `scanner/detect/` | scanner decision | Fusion factors, phase, live detection |
| `scanner/gate/` | scanner decision | Delivery gate pipeline |
| `scanner/setups/` | scanner decision | Catalog detectors (lab EV) |
| `scanner/delivery/` | scanner decision | Arbiter, lab lane, cooldown state |
| `scanner/playbook/` | scanner decision | Re-export analysis playbook |
| `scanner/forecast/` | scanner decision | Re-export maps.forecast |
| `scanner/telegram.py` | scanner transport | RU macquette Module 2 |
| `deep/verdict_v2/` | deep decision | Verdict V2 engines (migrated from `analysis/deep/`) |
| `deep/build.py`, `deep/pinned.py` | deep decision | Pinned + on-demand analysis orchestration |
| `deep/engine.py` | deep decision | Public façade |
| `deep/signal.py` | deep decision | BTC/MTF/liquidity/order-flow/POC helpers (ex `analysis/deep_signal`) |
| `analysis/deep_signal.py` | shim | Re-export → `hunt_core.deep.signal` |
| `analysis/deep/__init__.py` | shim | Re-export → `hunt_core.deep` |
| `maps/` | shared facts input | Orderbook/VP/liq maps — no standalone prod TG |
| `runtime/` | orchestration | Watch loops — no decision logic |
| `deliver/` | transport | Dispatch + legacy formatters (migrate to module TG) |

## Cross-module edges

- None allowlisted — `check_imports` enforces `deep⊥scanner`, `analysis⊥scanner` (except `scanner/playbook/` shim).
- `analysis/deep_signal` has no scanner imports (order flow via `shared/facts`).

## Dependency rule

- `shared/*` must not import `scanner/*` or `deep/*` (except `shared/mathlib` re-export transition)
- `deep/*` must not import `scanner/*` decision modules
- `scanner/*` must not import `deep/*` decision modules

Lint: `python -m hunt_core._dev.check_imports`
