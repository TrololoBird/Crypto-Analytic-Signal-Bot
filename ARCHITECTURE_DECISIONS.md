# ARCHITECTURE DECISIONS

## ADR-001: Signal-only System

The bot is analytical only. It uses Binance public REST/WS data and Telegram delivery.
It must not contain order placement, private account endpoints, or auto-trading execution.

## ADR-002: Contract Before Delivery

Every candidate signal must pass `bot/signal_contract.py` before it can enter the delivery
selection path. Invalid entries, missing stop loss, missing TP levels, bad ordering, or
insufficient TP1 R:R are rejected before repository, cooldown, or Telegram calls.

## ADR-003: Hard Confluence Gate

The score model ranks signals but does not authorize delivery by itself. Delivery requires
at least 3 independent confirmations from trend, momentum, volume, HTF alignment, and
microstructure. A single extreme score component must not send a weak signal.

## ADR-004: Live-safe Swing and Pivot Detection

Swing points and shared pivot detectors must not use `shift(-N)` or future bars. Confirmation
can occur only when the confirming bar is already closed/available to the live process.

## ADR-005: Union Shortlist

The shortlist is `pinned symbols + active signal symbols + scored dynamic candidates`.
Pinned symbols are always present. Dynamic symbols are selected by multifactor quality,
not alphabetic order or raw volume alone.

## ADR-006: ATR/Volatility-aware Risk

Strategies and contract validation must keep SL/TP levels volatility-aware and ordered.
Fixed percent levels are not enough across BTC, majors, high-beta alts, and metals.

## ADR-007: Explicit Skips For Auditability

Strategies that are inactive due to session schedule must emit an explicit skip result.
Silent omission makes the audit think fewer than 38 strategies were evaluated.

## ADR-008: Local Hooks Plus Tracked Copies

Git does not track `.git/hooks`. Local hooks are installed for this checkout, and identical
tracked copies are kept in `scripts/git-hooks/` for future agents.

## ADR-009: Rolling Historical Audit Is A Gate

Live snapshots can miss rare but valid detectors. A strategy is treated as audit-clean only
when it can run in rolling closed-candle replay without runtime errors, without signal
contract failures, and without zero hits across the configured top-symbol window.

## ADR-010: Recent Closed-bar Detectors May Lag, But Must Not Drift

Impulse, ATR expansion, aggression shift, and stop-hunt style detectors may emit from a
bounded recent closed-bar window. They must never inspect future bars, and they must reject
stale candidates when current price has drifted too far from the event by ATR.
