# Audit Forensics — Session 20260530

Generated from `python scripts/forensic_session.py` and log review.

## Session identity

| Field | Value |
|-------|-------|
| PID | 13836 |
| Log | `data/bot/logs/bot_20260530_020949_13836.log` |
| Telemetry run | `data/bot/telemetry/runs/20260530_020949_13836` |

## Top rejection reasons (telemetry tail)

| Count | Reason |
|------:|--------|
| 1204 | `stale_15m` |
| 132 | `targets.stop_hunt_not_detected` |
| 118 | `indicator.atr_expansion_too_low` |
| 118 | `indicator.no_bb_kc_squeeze` |
| 106 | `pattern.absorption_not_confirmed` |

`stale_15m` is the dominant filter rejection (~16% of rejected rows). Root cause: freshness compared raw age from last **closed** bar without accounting for the 15m candle boundary after incomplete-tail drops. Fixed in `bot/filters.py` via boundary-aware `_frame_is_fresh`.

## SQLite outcomes (332 total)

| Result | Count |
|--------|------:|
| expired_active | 113 |
| expired_pending | 96 |
| stop_loss | 80 |
| tp2_hit | 20 |
| superseded | 8 |

32 active signals at time of report.

## Log errors (`bot_error.txt`, startup 2026-05-29)

- `fetch_exchange_symbols` failed (2 attempts) → shortlist refresh fell back to **pinned_fallback**
- Frame fetch failures for BTCUSDT, SOLUSDT, ETHUSDT during cold start
- `memory market context update failed` once after REST errors

No `cycle_timeout` or `Unhandled exception` in the latest session log grep.

## Dashboard HTTP probe

Bot was **not running** at audit time (`127.0.0.1:8765` and `:8080` connection refused). Re-run when bot is live:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/api/status
Invoke-WebRequest http://127.0.0.1:8080/api/health
```

## Remediation status

| Item | Status |
|------|--------|
| Local fix batches committed & pushed | Done (32121b8..f7d2539) |
| `stale_15m` boundary freshness | Fixed + tests |
| `_FrameCache` RLock | Already non-blocking `threading.Lock` in main |
| Shortlist pinned fallback | `build_pinned_shortlist` sets `strategy_fits` + `shortlist_score=1.0` |
| Dashboard MAE/MFE/R KPIs | Wired via `/api/analytics/report` |
| Dashboard security tests | `tests/test_dashboard_security.py` |
