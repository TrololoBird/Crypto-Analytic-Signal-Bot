"""Strict data completeness gate for hunt analytics — no silent None/NaN/gaps.

Financial signal paths must not score or emit scenarios on partial data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from hunt_core.data_readiness import effective_prepared_minimums, raw_frame_minimums
from hunt_core.domain.config import BotSettings

# Canonical indicator columns from full prepare (active_groups=None) minus raw OHLCV.
# Exactly one side is populated per bar (trend direction); XOR completeness check.
INDICATOR_XOR_PAIRS: tuple[tuple[str, str], ...] = (("psar_long", "psar_short"),)

FULL_INDICATOR_COLUMNS: frozenset[str] = frozenset(
    {
        "adx14",
        "aroon_down14",
        "aroon_osc14",
        "aroon_up14",
        "atr14",
        "atr_pct",
        "bb_pct_b",
        "bb_width",
        "bb_width_pctile50",
        "candle_bearish_engulfing",
        "candle_bullish_engulfing",
        "candle_doji",
        "candle_dragonfly",
        "candle_gravestone",
        "cci20",
        "chandelier_dir",
        "chandelier_long",
        "chandelier_short",
        "chikou",
        "close_ols_slope20",
        "close_ols_slope_atr20",
        "close_ols_slope_pct20",
        "close_position",
        "cmf20",
        "delta_ratio",
        "donchian_high20",
        "donchian_low20",
        "ema20",
        "ema200",
        "ema50",
        "fisher",
        "fisher_signal",
        "hma21",
        "hma9",
        "kama10",
        "kc_lower",
        "kc_upper",
        "kc_width",
        "kijun",
        "macd_hist",
        "macd_line",
        "macd_signal",
        "mfi14",
        "microprice_deviation_pct",
        "minus_di14",
        "obv",
        "obv_above_ema",
        "obv_ema20",
        "pivot_point",
        "pivot_r1",
        "pivot_r2",
        "pivot_s1",
        "pivot_s2",
        "plus_di14",
        "prev_donchian_high20",
        "prev_donchian_low20",
        "psar_long",
        "psar_reversal",
        "psar_short",
        "realized_vol_20",
        "roc10",
        "rolling_cvd_24h",
        "rsi14",
        "senkou_a",
        "senkou_b",
        "session_asia",
        "session_asia_vol_20",
        "session_cvd",
        "session_london",
        "session_london_vol_20",
        "session_ny",
        "session_ny_vol_20",
        "session_overlap",
        "session_overlap_vol_20",
        "signed_order_flow",
        "slope5",
        "squeeze_hist",
        "squeeze_no",
        "squeeze_off",
        "squeeze_on",
        "stoch_d14",
        "stoch_h14",
        "stoch_k14",
        "stoch_rsi14",
        "supertrend",
        "supertrend_dir",
        "tenkan",
        "tob_imbalance",
        "uo",
        "volume_mean20",
        "volume_profile",
        "volume_profile_vah",
        "volume_profile_val",
        "volume_ratio20",
        "vwap",
        "vwap_deviation_atr14",
        "vwap_deviation_pct",
        "vwap_deviation_z20",
        "vwap_lower1",
        "vwap_lower2",
        "vwap_std",
        "vwap_upper1",
        "vwap_upper2",
        "willr14",
        "zscore30",
    }
)

REQUIRED_OHLCV: frozenset[str] = frozenset({"open", "high", "low", "close", "volume"})

REQUIRED_REST_SCALAR_KEYS: tuple[str, ...] = (
    "oi",
    "oi_chg_5m",
    "oi_chg_1h",
    "ls_5m",
    "ls_1h",
    "top_ls_5m",
    "top_ls_1h",
    "global_ls_5m",
    "global_ls_1h",
    "taker_5m",
    "taker_15m",
    "taker_1h",
    "funding",
    "basis_5m",
)

REQUIRED_BOOK_KEYS: tuple[str, ...] = ("bid_price", "ask_price", "bid_qty", "ask_qty")

MIN_SERIES_LEN = 12
GAP_CHECK_TAIL = 80

TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


class DataIncompleteError(Exception):
    """Raised when required market/analytics inputs are missing or non-finite."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__(f"data incomplete ({len(violations)}): " + "; ".join(violations[:8]))


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    complete: bool
    violations: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **details: Any) -> CompletenessReport:
        return cls(complete=True, details=details)

    @classmethod
    def fail(cls, violations: list[str], **details: Any) -> CompletenessReport:
        return cls(complete=False, violations=tuple(violations), details=details)


def finite_float(value: object, *, field: str) -> float:
    if value is None:
        raise DataIncompleteError((f"{field}=null",))
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise DataIncompleteError((f"{field}=not_numeric",)) from exc
    if not math.isfinite(numeric):
        raise DataIncompleteError((f"{field}=non_finite",))
    return numeric


def _cell_finite(df: Any, column: str, idx: int, *, ctx: str) -> float | None:
    if column not in df.columns:
        return None
    try:
        v = float(df.item(idx, column))
    except (TypeError, ValueError, IndexError):
        return None
    if not math.isfinite(v):
        return None
    return v


def audit_kline_frame(
    df: Any,
    *,
    tf: str,
    symbol: str,
    min_raw_bars: int,
    min_prepared_bars: int,
) -> list[str]:
    violations: list[str] = []
    if df is None:
        return [f"klines.{tf}.missing_frame"]
    if df.is_empty():
        return [f"klines.{tf}.empty_frame"]
    height = int(df.height)
    if height < min_raw_bars:
        violations.append(f"klines.{tf}.rows={height}<min_raw={min_raw_bars}")
    if height < min_prepared_bars:
        violations.append(f"klines.{tf}.rows={height}<min_prepared={min_prepared_bars}")

    for col in REQUIRED_OHLCV:
        if col not in df.columns:
            violations.append(f"klines.{tf}.missing_column.{col}")

    for bar_label, idx in (("live", -1), ("closed", -2)):
        if bar_label == "closed" and height < 2:
            violations.append(f"klines.{tf}.closed_bar_unavailable")
            continue
        for col in REQUIRED_OHLCV:
            if _cell_finite(df, col, idx, ctx=f"{tf}.{bar_label}.{col}") is None:
                violations.append(f"klines.{tf}.{bar_label}.{col}=invalid")

    time_col = next((c for c in ("close_time", "time", "open_time") if c in df.columns), None)
    if time_col is None:
        violations.append(f"klines.{tf}.missing_time_column")
        return violations

    interval_ms = TF_MS.get(tf)
    if interval_ms is None:
        return violations

    tail = min(GAP_CHECK_TAIL, height - 1)
    if tail >= 2:
        times: list[int] = []
        for i in range(height - tail, height):
            raw_t = df.item(i, time_col)
            try:
                if hasattr(raw_t, "timestamp"):
                    times.append(int(raw_t.timestamp() * 1000))
                else:
                    times.append(int(raw_t))
            except (TypeError, ValueError):
                violations.append(f"klines.{tf}.time_parse_failed")
                return violations
        for i in range(1, len(times)):
            delta = times[i] - times[i - 1]
            if delta > interval_ms * 1.5:
                missed = max(1, round(delta / interval_ms) - 1)
                violations.append(
                    f"klines.{tf}.gap.{symbol}.{missed}bars@{times[i - 1]}->{times[i]}"
                )
    return violations


def audit_prepared_indicators(
    df: Any,
    *,
    tf: str,
    bar_label: str,
    idx: int,
) -> list[str]:
    violations: list[str] = []
    if df is None or df.is_empty():
        return [f"indicators.{tf}.{bar_label}.no_frame"]

    missing_cols = sorted(FULL_INDICATOR_COLUMNS - set(df.columns))
    if missing_cols:
        violations.append(
            f"indicators.{tf}.{bar_label}.missing_columns={','.join(missing_cols[:6])}"
            + (f"+{len(missing_cols) - 6}more" if len(missing_cols) > 6 else "")
        )
        return violations

    xor_cols = {c for pair in INDICATOR_XOR_PAIRS for c in pair}
    bad: list[str] = []
    for col in sorted(FULL_INDICATOR_COLUMNS):
        if col in xor_cols:
            continue
        if _cell_finite(df, col, idx, ctx=f"{tf}.{bar_label}.{col}") is None:
            bad.append(col)
    for left, right in INDICATOR_XOR_PAIRS:
        if _cell_finite(df, left, idx, ctx=left) is None and _cell_finite(
            df, right, idx, ctx=right
        ) is None:
            bad.append(f"xor:{left}|{right}")
    if bad:
        violations.append(
            f"indicators.{tf}.{bar_label}.non_finite={','.join(bad[:8])}"
            + (f"+{len(bad) - 8}more" if len(bad) > 8 else "")
        )
    return violations


def audit_rest_pack(pack: dict[str, Any], *, symbol: str) -> list[str]:
    violations: list[str] = []
    for key in REQUIRED_REST_SCALAR_KEYS:
        val = pack.get(key)
        if val is None:
            violations.append(f"rest.{key}=null")
            continue
        try:
            if not math.isfinite(float(val)):
                violations.append(f"rest.{key}=non_finite")
        except (TypeError, ValueError):
            violations.append(f"rest.{key}=not_numeric")

    book = pack.get("book_depth")
    if not isinstance(book, dict):
        violations.append("rest.book_depth=missing")
    else:
        for key in REQUIRED_BOOK_KEYS:
            if book.get(key) is None:
                violations.append(f"rest.book.{key}=null")
            else:
                try:
                    if not math.isfinite(float(book[key])):
                        violations.append(f"rest.book.{key}=non_finite")
                except (TypeError, ValueError):
                    violations.append(f"rest.book.{key}=not_numeric")

    agg = pack.get("agg_trades")
    if agg is None:
        violations.append("rest.agg_trades=null")
    else:
        delta = getattr(agg, "delta_ratio", None)
        if delta is None or not math.isfinite(float(delta)):
            violations.append("rest.agg_trades.delta_ratio=invalid")

    for series_key in ("oi_series", "gls_series"):
        series = pack.get(series_key)
        if not isinstance(series, list) or len(series) < MIN_SERIES_LEN:
            violations.append(f"rest.{series_key}.len<{MIN_SERIES_LEN}")
            continue
        for i, point in enumerate(series):
            try:
                if not math.isfinite(float(point)):
                    violations.append(f"rest.{series_key}[{i}]=non_finite")
                    break
            except (TypeError, ValueError):
                violations.append(f"rest.{series_key}[{i}]=not_numeric")
                break

    if violations:
        violations.insert(0, f"rest_pack.{symbol}")
    return violations


def audit_ticker(ticker: dict[str, Any] | None, *, symbol: str) -> list[str]:
    if ticker is None:
        return [f"ticker.{symbol}=missing"]
    violations: list[str] = []
    for key in ("last_price", "quote_volume", "price_change_percent"):
        val = ticker.get(key)
        if val is None:
            violations.append(f"ticker.{key}=null")
        else:
            try:
                if not math.isfinite(float(val)):
                    violations.append(f"ticker.{key}=non_finite")
            except (TypeError, ValueError):
                violations.append(f"ticker.{key}=not_numeric")
    return violations


def audit_beat_dump_tick(
    *,
    symbol: str,
    ticker: dict[str, Any] | None,
    kline_map: dict[str, Any],
    prepared_map: dict[str, Any],
    pack: dict[str, Any],
    settings: BotSettings,
    tf_keys: tuple[str, ...],
) -> CompletenessReport:
    violations: list[str] = []
    violations.extend(audit_ticker(ticker, symbol=symbol))
    violations.extend(audit_rest_pack(pack, symbol=symbol))

    raw_min = raw_frame_minimums(settings)
    prep_min = effective_prepared_minimums(settings)
    raw_min.setdefault("1m", 300)
    raw_min.setdefault("3m", 200)
    prep_min.setdefault("1m", 100)
    prep_min.setdefault("3m", 80)

    frame_rows: dict[str, int] = {}
    indicator_counts: dict[str, int] = {}

    for tf in tf_keys:
        raw = kline_map.get(tf)
        prep = prepared_map.get(tf)
        frame_rows[tf] = int(raw.height) if raw is not None and not raw.is_empty() else 0
        violations.extend(
            audit_kline_frame(
                raw,
                tf=tf,
                symbol=symbol,
                min_raw_bars=int(raw_min.get(tf, raw_min.get("5m", 100))),
                min_prepared_bars=int(prep_min.get(tf, prep_min.get("5m", 80))),
            )
        )
        for bar_label, idx in (("live", -1), ("closed", -2)):
            if prep is None or prep.is_empty():
                violations.append(f"indicators.{tf}.{bar_label}.no_prepared_frame")
                continue
            if bar_label == "closed" and prep.height < 2:
                violations.append(f"indicators.{tf}.closed_bar_unavailable")
                continue
            ind_v = audit_prepared_indicators(prep, tf=tf, bar_label=bar_label, idx=idx)
            violations.extend(ind_v)
        if prep is not None and not prep.is_empty():
            indicator_counts[tf] = len(FULL_INDICATOR_COLUMNS)

    if violations:
        return CompletenessReport.fail(
            violations,
            symbol=symbol,
            frame_rows=frame_rows,
            indicator_columns_expected=len(FULL_INDICATOR_COLUMNS),
            indicator_counts=indicator_counts,
        )
    return CompletenessReport.ok(
        symbol=symbol,
        frame_rows=frame_rows,
        indicator_columns=len(FULL_INDICATOR_COLUMNS),
        indicator_counts=indicator_counts,
    )


def series_z_strict(values: list[float], *, field: str) -> float:
    if len(values) < MIN_SERIES_LEN:
        raise DataIncompleteError((f"{field}.len<{MIN_SERIES_LEN}",))
    base = [float(x) for x in values[:-1]]
    mean = sum(base) / len(base)
    # Sample variance (ddof=1): the baseline window is a sample, not the population.
    var = sum((x - mean) ** 2 for x in base) / max(len(base) - 1, 1)
    std = var**0.5
    if std <= 0:
        raise DataIncompleteError((f"{field}.zero_variance",))
    last = float(values[-1])
    if not math.isfinite(last):
        raise DataIncompleteError((f"{field}.last_non_finite",))
    return round((last - mean) / std, 4)


def series_chg_pct_strict(values: list[float], *, field: str) -> float:
    if len(values) < 2:
        raise DataIncompleteError((f"{field}.len<2",))
    first = float(values[0])
    last = float(values[-1])
    if not math.isfinite(first) or not math.isfinite(last):
        raise DataIncompleteError((f"{field}.non_finite",))
    if first == 0:
        raise DataIncompleteError((f"{field}.zero_baseline",))
    return round((last / first - 1.0) * 100.0, 4)
