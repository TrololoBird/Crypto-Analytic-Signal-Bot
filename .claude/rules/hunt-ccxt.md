# Hunter — CCXT mandatory

When editing anything under `hunt/`, especially `hunt_core/market/`:

1. **Read skill `ccxt-python`** (`.claude/skills/ccxt-python/SKILL.md` or `/ccxt-python`)
2. **Read skill `hunt-ccxt`** (`.claude/skills/hunt-ccxt/SKILL.md`)
3. **Read canon** `hunt/docs/CCXT.md`

Do not invent raw Binance FAPI HTTP in `hunt/hunt_core/market/` — use CCXT unified + implicit API per `hunt/docs/CCXT.md` (100% CCXT market plane).

Public data only. `binance` + `defaultType: future`. `enableRateLimit: True`.

- REST gate: `ccxt_method_available()` from `ccxt_guard.py`
- Pro WS gate: `ccxt_ws_method_available()` — Binance primary funding via `watchMarkPrices` only
- CI: `python -m hunt_core._dev.check_ccxt`
