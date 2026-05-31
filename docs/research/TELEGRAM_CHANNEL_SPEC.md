# Telegram Channel Product Spec

Tiered public channel for **manual** futures trading on Binance USD-M. No auto-trading, no API keys in bot.

## Channel modes

| Mode | Audience | Content |
|------|----------|---------|
| Free preview | Public | 1–2 ACTION/day or 15m delay; full format |
| Main | Subscribers | ACTION + tracking |
| WATCH | Optional | Setup forming, silent |
| Daily digest | Pinned | Regime, OI/liq, 24h stats |
| Post-mortem | Thread | SL review with chart |

**Chosen model:** Tiered — WATCH (many, silent) + ACTION (15–40/day soft, 8–15 burst per 15m).

**Web rationale:** quality-first manual channels чаще **3–8** (CryptoSignalsPro) до **8–15** (FX aggregators); **50+/day** — маркетинг/high-leverage anti-pattern для ручного входа. См. [WEB_RESEARCH_SUPPLEMENT.md](WEB_RESEARCH_SUPPLEMENT.md) §4.

## Telegram API limits (delivery design)

| Limit | Value | Implication |
|-------|-------|-------------|
| Per chat | ~1 msg/s | Serialize ACTION + updates per channel |
| Global | ~30 msg/s | Enough for 40 ACTION + tracking if staggered |
| Supergroup | ~20 msg/min | Prefer **channel** for high WATCH volume |
| 429 | `retry_after` | Mandatory queue in `messaging` layer |

Sources: [python-telegram-bot FloodLimit](https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.7/telegram/constants.py), [Binance-independent TG guides](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Avoiding-flood-limits).

## Cadence limits

| Type | Limit | Rationale |
|------|-------|-----------|
| ACTION | 15–40 / day | Human execution |
| WATCH | 50–120 / day | Radar only |
| Per symbol ACTION | 1 / 2–4h | Anti-spam |
| Per setup_id | 1 / global 15m cycle | Diversity |
| Tracking updates | No hard daily cap | Open plans |

## Signal JSON (internal + audit)

```json
{
  "signal_id": "uuid",
  "symbol": "BTCUSDT",
  "direction": "long",
  "setup_type": "fvg_setup",
  "timeframe": "15m",
  "tier": "action",
  "entry": { "low": 95000, "high": 95200, "weights": [0.5, 0.3, 0.2] },
  "stop_loss": 94500,
  "take_profits": [95800, 96500, 97800],
  "risk_reward_tp1": 1.9,
  "valid_until": "ISO8601",
  "invalidation": "15m close below 94400",
  "reasons": ["fvg_touched", "htf_uptrend", "volume_confirm"],
  "regime": { "market": "trending", "btc_bias": "bull" },
  "data_snapshot_ts": "ISO8601"
}
```

Compatible with [telegram-crypto-signal-parser](https://github.com/joostmbakker/telegram-crypto-signal-parser) field ideas.

## HTML templates

### ACTION

```text
[SIGNAL] BTCUSDT LONG #a1b2c3
Setup: FVG retest · 15m · Confidence: B+
Type: Limit scale-in (3 legs) — manual only

Entry zone: 95,000 – 95,200
Stop: 94,500 (−0.53%)
Targets: TP1 95,800 (1.9R) | TP2 96,500 | TP3 97,800
TTL: valid until 2026-06-01 14:30 UTC

Why: FVG mitigated; 1h structure up; volume > 1.2× avg
Context: Funding neutral | OI +2.1% | BTC bias bull
Invalidate if: 15m close below 94,400

Chart: https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT.P
Disclaimer: Education only. Not financial advice. No auto-trading.
```

### TRACKING

```text
[TP1 HIT] BTCUSDT LONG #a1b2c3 @ 95,810
Suggestion: optional move SL to breakeven
```

### WATCH (silent)

```text
[WATCH] SOLUSDT — OB 142.5–143.0 forming
No entry. ACTION if 15m closes above 143.2 with volume.
```

## Notification policy

Inspired by [Freqtrade telegram settings](https://docs.freqtrade.io/en/2026.3/telegram-usage/):

| Event | Default |
|-------|---------|
| ACTION new | on |
| WATCH | silent |
| TP/SL update | on |
| Daily digest | silent |
| Health ops | off (dashboard only) |

## Trust requirements

1. Publish losses in tracking channel or digest.
2. Verifiable audit log (CSV + hash) — see innovation backlog.
3. Anti-front-run: ACTION only on **candle close** of `trigger_tf` (configurable).
4. No win-rate guarantees; always show SL.

## Confluence display (companion / footer)

```text
Confluence 4/5: trend ✓ structure ✓ volume ✓ funding ✓ micro ✗
```

## Disclaimer (fixed footer)

Education and market commentary only. Not financial advice. Past performance does not guarantee future results. You trade manually at your own risk. This bot does not access your exchange account.
