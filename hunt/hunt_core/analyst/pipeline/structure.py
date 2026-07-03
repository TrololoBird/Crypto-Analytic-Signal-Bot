from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float
from hunt_core.analyst.pipeline.types import ModuleResult


def _resolve_ohlcv(row: dict[str, Any], tf_key: str) -> list[dict[str, float]]:
    prep = row.get("_prepared")
    if prep is not None:
        work = getattr(prep, "work_4h", None)
        if work is not None and hasattr(work, "height") and work.height >= 4:
            try:
                tail = work.tail(24)
                closes = tail["close"].to_list()
                highs = tail["high"].to_list()
                lows = tail["low"].to_list()
                return [
                    {"high": highs[i], "low": lows[i], "close": closes[i]}
                    for i in range(len(closes))
                ]
            except Exception:
                pass

    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    snap = tf.get(tf_key) or {}
    ohlcv_raw = snap.get("ohlcv")
    if isinstance(ohlcv_raw, list) and len(ohlcv_raw) >= 4:
        result = []
        for bar in ohlcv_raw:
            if isinstance(bar, dict):
                h = safe_float(bar.get("high"))
                l = safe_float(bar.get("low"))
                c = safe_float(bar.get("close"))
                if h > 0 and l > 0 and c > 0:
                    result.append({"high": h, "low": l, "close": c})
        return result
    return []


LOOKBACK_PIVOT = 5
BOS_BUFFER = 0.003
LOOKBACK_HH_LL = 20


def _detect_structure(bars: list[dict[str, float]]) -> dict[str, Any]:
    if len(bars) < 4:
        return {}

    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    p2 = bars[-3] if len(bars) >= 3 else None
    p3 = bars[-4] if len(bars) >= 4 else None

    hh = None
    hl = None
    lh = None
    ll = None

    if prev and p2 and p3 and len(bars) >= 5:
        p4 = bars[-5] if len(bars) >= 5 else None
        if p4:
            hh = (last["high"] > prev["high"] > p2["high"] > p3["high"]
                  and prev["high"] > p2["high"] and p2["high"] > p3["high"])
            hl = (last["low"] > prev["low"] > p2["low"] > p3["low"]
                  and prev["low"] > p2["low"] and p2["low"] > p3["low"])
            lh = (last["high"] < prev["high"] < p2["high"] < p3["high"]
                  and prev["high"] < p2["high"] and p2["high"] < p3["high"])
            ll = (last["low"] < prev["low"] < p2["low"] < p3["low"]
                  and prev["low"] < p2["low"] and p2["low"] < p3["low"])
        else:
            hh = last["high"] > prev["high"] > p2["high"]
            hl = last["low"] > prev["low"] > p2["low"]
            lh = last["high"] < prev["high"] < p2["high"]
            ll = last["low"] < prev["low"] < p2["low"]

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    lookback = min(LOOKBACK_HH_LL, len(bars))

    hh_last = max(highs[-lookback:]) if len(highs) >= lookback else max(highs)
    ll_last = min(lows[-lookback:]) if len(lows) >= lookback else min(lows)
    hl_last = None
    lh_last = None

    for i in range(-lookback, 0):
        if i - LOOKBACK_PIVOT + 1 < -1:
            continue
        if lows[i] > lows[i - 1] > lows[i - 2] and lows[i - 1] < lows[i - 2] and lows[i - 1] < lows[i - 3]:
            hl_last = lows[i]
        if highs[i] < highs[i - 1] < highs[i - 2] and highs[i - 1] > highs[i - 2] and highs[i - 1] > highs[i - 3]:
            lh_last = highs[i]

    close = last["close"]
    prev_close = prev["close"] if prev else close

    bos_up = prev_close <= hh_last and close > hh_last * (1 + BOS_BUFFER)
    bos_down = prev_close >= ll_last and close < ll_last * (1 - BOS_BUFFER)
    choch_bull = prev_close <= (lh_last or 0) and (lh_last is not None) and close > lh_last * (1 + BOS_BUFFER)
    choch_bear = prev_close >= (hl_last or 0) and (hl_last is not None) and close < hl_last * (1 - BOS_BUFFER)

    return {
        "hh": hh,
        "hl": hl,
        "lh": lh,
        "ll": ll,
        "bos_up": bos_up,
        "bos_down": bos_down,
        "choch_bull": choch_bull,
        "choch_bear": choch_bear,
        "close": close,
        "prev_close": prev_close,
        "hh_last": hh_last,
        "ll_last": ll_last,
        "hl_last": hl_last,
        "lh_last": lh_last,
        "bar_count": len(bars),
    }


def run_structure_module(row: dict[str, Any], direction: str = "long") -> ModuleResult:
    tf_key = "4h_closed" if row.get("timeframes", {}).get("4h_closed") else "4h"
    bars = _resolve_ohlcv(row, tf_key)

    if len(bars) < 4:
        return ModuleResult(
            status="UNKNOWN",
            reason=f"Недостаточно баров для анализа структуры ({len(bars)}<4)",
            details={"tf_key": tf_key, "bar_count": len(bars)},
        )

    s = _detect_structure(bars)
    evidence: list[str] = []
    for k, v in s.items():
        if k in ("close", "prev_close", "bar_count"):
            continue
        evidence.append(f"{k}={v}")

    if direction == "long":
        bullish = s.get("hl") and s.get("bos_up")
        choch_up = s.get("choch_bull")
        if bullish:
            return ModuleResult(
                status="PASS",
                reason="HL + BOS вверх — бычья структура",
                details={"structure": s, "evidence": evidence},
            )
        if choch_up:
            return ModuleResult(
                status="PASS",
                reason="CHoCH вверх — смена характера на бычий",
                details={"structure": s, "evidence": evidence},
            )
        if s.get("hl"):
            return ModuleResult(
                status="CAUTION",
                reason="HL сформирован, но BOS не подтверждён",
                details={"structure": s, "evidence": evidence},
            )
        return ModuleResult(
            status="FAIL",
            reason=f"Структура не для лонга: нет HL/BOS",
            details={"structure": s, "evidence": evidence},
        )

    bearish = s.get("lh") and s.get("bos_down")
    choch_down = s.get("choch_bear")
    if bearish:
        return ModuleResult(
            status="PASS",
            reason="LH + BOS вниз — медвежья структура",
            details={"structure": s, "evidence": evidence},
        )
    if choch_down:
        return ModuleResult(
            status="PASS",
            reason="CHoCH вниз — смена характера на медвежий",
            details={"structure": s, "evidence": evidence},
        )
    if s.get("lh"):
        return ModuleResult(
            status="CAUTION",
            reason="LH сформирован, но BOS не подтверждён",
            details={"structure": s, "evidence": evidence},
        )
    return ModuleResult(
        status="FAIL",
        reason="Структура не для шорта: нет LH/BOS",
        details={"structure": s, "evidence": evidence},
    )
