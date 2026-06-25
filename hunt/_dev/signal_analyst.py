"""Independent signal analyst — runs alongside hunt watch.

Tails hunt_scan.jsonl for confirmed/delivered signals, performs independent
technical analysis via CCXT, compares with hunter assessment, and writes
divergences to data/analyst_divergences.jsonl.

Usage:
    .venv/bin/python -m hunt_core._dev.signal_analyst --minutes 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── paths ──────────────────────────────────────────────────────────────────
HUNT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HUNT))

from hunt_core.paths import HUNT_SCAN_JSONL, DATA

DIVERGENCE_FILE = DATA / "analyst_divergences.jsonl"
ANALYST_LOG = DATA / "analyst_session.jsonl"

# ── thresholds ─────────────────────────────────────────────────────────────
# Считаем расхождение, если разница confidence > 0.25 ИЛИ направление разное
CONFIDENCE_DIVERGENCE_THRESHOLD = 0.25
# Считаем mid-leg (позднее движение) если цена уже ушла > N% от базы
LATE_ENTRY_PCT = 4.0
# Минимальное число баров для анализа
MIN_BARS = 20


def _log(msg: str) -> None:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"{ts} | analyst | {msg}", flush=True)


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── independent technical analysis ────────────────────────────────────────

async def _fetch_ohlcv(client: Any, symbol: str, timeframe: str = "1h", limit: int = 40) -> list:
    try:
        return await client.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as exc:
        _log(f"ohlcv_fetch_failed symbol={symbol} err={exc!r}")
        return []


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _volume_z(volumes: list[float]) -> float | None:
    """Z-score of last bar volume vs trailing window."""
    if len(volumes) < MIN_BARS:
        return None
    window = volumes[-MIN_BARS:-1]
    mean = sum(window) / len(window)
    variance = sum((v - mean) ** 2 for v in window) / len(window)
    std = variance ** 0.5
    if std == 0:
        return 0.0
    return (volumes[-1] - mean) / std


def _momentum_pct(closes: list[float], lookback: int = 8) -> float | None:
    if len(closes) < lookback + 1:
        return None
    base = closes[-(lookback + 1)]
    if base == 0:
        return None
    return (closes[-1] - base) / base * 100


def _independent_assessment(ohlcv: list) -> dict:
    """Return independent view: direction, confidence (0-1), flags."""
    if len(ohlcv) < MIN_BARS:
        return {"valid": False, "reason": "insufficient_bars"}

    closes = [bar[4] for bar in ohlcv]
    volumes = [bar[5] for bar in ohlcv]
    highs = [bar[2] for bar in ohlcv]
    lows = [bar[3] for bar in ohlcv]

    rsi = _compute_rsi(closes)
    vol_z = _volume_z(volumes)
    mom_8h = _momentum_pct(closes, 8)
    mom_24h = _momentum_pct(closes, 24) if len(closes) >= 25 else None

    # Trend via simple MA crossover
    ma_fast = sum(closes[-8:]) / 8
    ma_slow = sum(closes[-20:]) / 20 if len(closes) >= 20 else ma_fast
    trend_long = ma_fast > ma_slow

    # Volatility: ATR (simplified)
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(ohlcv))
    ]
    atr = sum(true_ranges[-14:]) / 14 if len(true_ranges) >= 14 else None
    atr_pct = (atr / closes[-1] * 100) if (atr and closes[-1] > 0) else None

    # Direction signal
    long_signals = 0
    short_signals = 0
    flags = []

    if rsi is not None:
        if rsi < 35:
            long_signals += 2
            flags.append(f"rsi_oversold={rsi:.1f}")
        elif rsi > 65:
            short_signals += 2
            flags.append(f"rsi_overbought={rsi:.1f}")
        else:
            flags.append(f"rsi_neutral={rsi:.1f}")

    if trend_long:
        long_signals += 1
        flags.append("ma_trend_long")
    else:
        short_signals += 1
        flags.append("ma_trend_short")

    if vol_z is not None and vol_z > 1.5:
        # Volume spike — momentum confirmation
        if trend_long:
            long_signals += 1
            flags.append(f"vol_spike_confirms_long z={vol_z:.2f}")
        else:
            short_signals += 1
            flags.append(f"vol_spike_confirms_short z={vol_z:.2f}")

    if mom_8h is not None:
        if mom_8h > 3.0:
            short_signals += 1  # may be late long
            flags.append(f"mom_8h_extended_up={mom_8h:.1f}%")
        elif mom_8h < -3.0:
            long_signals += 1   # may be oversold
            flags.append(f"mom_8h_extended_down={mom_8h:.1f}%")

    total = long_signals + short_signals
    if total == 0:
        return {"valid": True, "direction": "neutral", "confidence": 0.5, "flags": flags,
                "rsi": rsi, "vol_z": vol_z, "mom_8h": mom_8h, "atr_pct": atr_pct}

    if long_signals >= short_signals:
        direction = "long"
        confidence = long_signals / total
    else:
        direction = "short"
        confidence = short_signals / total

    # Late entry penalty
    late_entry = (mom_8h is not None and abs(mom_8h) > LATE_ENTRY_PCT)
    if late_entry:
        flags.append(f"late_entry_risk mom_8h={mom_8h:.1f}%")
        confidence *= 0.7

    return {
        "valid": True,
        "direction": direction,
        "confidence": round(confidence, 3),
        "flags": flags,
        "rsi": round(rsi, 1) if rsi else None,
        "vol_z": round(vol_z, 2) if vol_z else None,
        "mom_8h": round(mom_8h, 2) if mom_8h else None,
        "mom_24h": round(mom_24h, 2) if mom_24h else None,
        "atr_pct": round(atr_pct, 3) if atr_pct else None,
        "late_entry": late_entry,
        "ma_trend": "long" if trend_long else "short",
    }


def _extract_hunter_view(record: dict) -> dict | None:
    """Pull direction, signal_type, score from a hunt_scan.jsonl row."""
    # Look for confirmed setup in long/dump
    for side in ("long", "dump"):
        setup = record.get(side)
        if not isinstance(setup, dict):
            continue
        if not setup.get("confirmed"):
            continue
        sig_type = setup.get("signal_type", "")
        if sig_type not in ("pre_phase", "mid_phase"):
            continue
        direction = "long" if side == "long" else "short"
        score = setup.get("score") or setup.get("gate_score")
        return {
            "direction": direction,
            "signal_type": sig_type,
            "score": float(score) if score else None,
            "phase": setup.get("phase"),
            "energy": setup.get("energy"),
            "structure": setup.get("structure"),
        }
    return None


def _detect_divergence(hunter: dict, analyst: dict) -> dict | None:
    """Return divergence record if views disagree meaningfully."""
    if not analyst.get("valid"):
        return None

    reasons = []

    # Direction mismatch
    if hunter["direction"] != analyst["direction"] and analyst["direction"] != "neutral":
        reasons.append(f"direction_mismatch hunter={hunter['direction']} analyst={analyst['direction']}")

    # Confidence gap
    hunter_conf = float(hunter.get("score") or 0.5)
    analyst_conf = analyst["confidence"]
    gap = abs(hunter_conf - analyst_conf)
    if gap > CONFIDENCE_DIVERGENCE_THRESHOLD:
        reasons.append(f"confidence_gap hunter={hunter_conf:.2f} analyst={analyst_conf:.2f} gap={gap:.2f}")

    # Late entry risk on confirmed hunter signal
    if analyst.get("late_entry"):
        reasons.append("analyst_sees_late_entry_risk")

    # RSI divergence: hunter says long but RSI overbought
    if hunter["direction"] == "long" and analyst.get("rsi") and analyst["rsi"] > 70:
        reasons.append(f"rsi_diverges_long rsi={analyst['rsi']}")
    elif hunter["direction"] == "short" and analyst.get("rsi") and analyst["rsi"] < 30:
        reasons.append(f"rsi_diverges_short rsi={analyst['rsi']}")

    if not reasons:
        return None

    return {"reasons": reasons, "severity": len(reasons)}


# ── tail logic ────────────────────────────────────────────────────────────

def _tail_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], offset
    if size <= offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read()
    new_offset = offset + len(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return [l for l in lines if l.strip()], new_offset


# ── main ──────────────────────────────────────────────────────────────────

async def run(minutes: int) -> None:
    import ccxt.async_support as ccxt

    client = ccxt.binance({
        "defaultType": "future",
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    deadline = time.monotonic() + minutes * 60
    offset = HUNT_SCAN_JSONL.stat().st_size if HUNT_SCAN_JSONL.exists() else 0
    seen_ids: set[str] = set()
    signal_count = 0
    divergence_count = 0

    _log(f"analyst started  watching={HUNT_SCAN_JSONL.name}  deadline={minutes}m")
    _log(f"divergences -> {DIVERGENCE_FILE}")

    try:
        while time.monotonic() < deadline:
            lines, offset = _tail_new_lines(HUNT_SCAN_JSONL, offset)
            for raw in lines:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                symbol = rec.get("symbol", "")
                ts = rec.get("ts") or rec.get("timestamp") or ""
                uid = f"{symbol}|{ts}"
                if uid in seen_ids:
                    continue

                hunter = _extract_hunter_view(rec)
                if hunter is None:
                    continue

                seen_ids.add(uid)
                signal_count += 1
                _log(f"signal #{signal_count} {symbol} {hunter['direction']} {hunter['signal_type']} score={hunter['score']}")

                # Independent analysis
                ohlcv = await _fetch_ohlcv(client, symbol, "1h", 40)
                analyst = _independent_assessment(ohlcv)

                divergence = _detect_divergence(hunter, analyst) if analyst.get("valid") else None

                session_rec = {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "signal_ts": ts,
                    "hunter": hunter,
                    "analyst": analyst,
                    "diverged": divergence is not None,
                }
                _append_jsonl(ANALYST_LOG, session_rec)

                if divergence is not None:
                    divergence_count += 1
                    div_rec = {
                        **session_rec,
                        "divergence": divergence,
                    }
                    _append_jsonl(DIVERGENCE_FILE, div_rec)
                    _log(
                        f"DIVERGENCE #{divergence_count} {symbol} "
                        f"reasons={divergence['reasons']} severity={divergence['severity']}"
                    )
                else:
                    _log(f"aligned {symbol} hunter={hunter['direction']} analyst={analyst.get('direction')} conf={analyst.get('confidence')}")

            await asyncio.sleep(5)
    finally:
        await client.close()
        _log(f"analyst done  signals={signal_count}  divergences={divergence_count}")
        if divergence_count > 0:
            _log(f"see {DIVERGENCE_FILE}")
        else:
            _log("no divergences found in this session")


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent signal analyst")
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(run(args.minutes))


if __name__ == "__main__":
    main()
