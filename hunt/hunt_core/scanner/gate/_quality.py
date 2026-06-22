"""Phase-quality delivery gates — migrated from delivery.py monolith."""
from __future__ import annotations

from typing import Any

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.scanner.gate._ev import setup_p_win
from hunt_core.scanner.gate._rr import (
    FADE_PHASES_SHORT,
    PUMP_PHASES_LONG,
    SHORT_DUMP_START_LC_PHASES,
    short_dump_delivery_too_late,
    short_dump_start_max_fall_pct,
    structural_dump_hard,
    structural_hard_count,
)
from hunt_core.scanner.gate._types import GateResult
from hunt_core.params.store import delivery_thresholds, universal_section


def passes_meme_anomaly_gate(
    *,
    sym: str,
    row: dict[str, Any],
    lc: dict[str, Any],
    cal: Any,
) -> bool:
    """Meme hunt volatility floor — no per-symbol incident waivers."""
    if sym in PINNED_SYMBOLS:
        return True
    if bool(row.get("young_listing")):
        return True
    chg24 = _row_chg24_abs(row)
    rng24 = _row_rng24(row)
    min_chg = float(cal.anomaly_min_chg_24h_pct)
    min_rng = float(cal.anomaly_min_range_24h_pct)
    return chg24 >= min_chg or rng24 >= min_rng


def _row_chg24_abs(row: dict[str, Any]) -> float:
    sess = row.get("session") or {}
    raw = (
        row.get("chg_24h_pct")
        or row.get("change_24h_pct")
        or sess.get("change_24h_pct")
    )
    return abs(float(raw or 0))


def _row_rng24(row: dict[str, Any]) -> float:
    sess = row.get("session") or {}
    return float(sess.get("range_pct_24h") or row.get("range_pct_24h") or 0)


_FADE_PHASES_SHORT = FADE_PHASES_SHORT
_PUMP_PHASES_LONG = PUMP_PHASES_LONG
_short_dump_start_max_fall_pct = short_dump_start_max_fall_pct
_short_dump_delivery_too_late = short_dump_delivery_too_late


def _setup_p_win(setup: dict[str, Any], *, confirmed: bool = True) -> float | None:
    p = setup_p_win(setup)
    if p is not None:
        return p
    return None


def check_delivery_confluence(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
) -> GateResult | None:
    dl = delivery_thresholds(symbol)
    p_win = _setup_p_win(setup)
    min_p = float(dl.get("min_p_win_forming", dl.get("min_p_win", 0.42)))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    phase = str(lifecycle.get("phase") or "")
    min_struct = int(dl.get("min_structural_hard", 2))
    struct_n = structural_hard_count(hard, direction=direction)
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    start_max = _short_dump_start_max_fall_pct(symbol)
    tf = row.get("timeframes") or {}
    has_div = bool(
        (tf.get("1h_closed") or tf.get("1h") or {}).get("bearish_rsi_div")
        or (tf.get("4h_closed") or tf.get("4h") or {}).get("bearish_rsi_div")
        or (tf.get("1h_closed") or tf.get("1h") or {}).get("bearish_macd_div")
        or (tf.get("4h_closed") or tf.get("4h") or {}).get("bearish_macd_div")
    )
    late_block = _short_dump_delivery_too_late(lifecycle, setup, symbol=symbol)
    if direction == "short" and late_block is not None:
        return late_block
    min_struct_eff = min_struct
    if (
        direction == "short"
        and phase in SHORT_DUMP_START_LC_PHASES
        and fall <= start_max
        and (p_win is None or p_win >= min_p)
        and (
            any("rejection" in str(h) for h in hard)
            or has_div
            or structural_dump_hard(hard)
        )
    ):
        min_struct_eff = 1
    if (
        direction == "short"
        and phase == "exhaustion_at_high"
        and (p_win is None or p_win >= min_p)
        and any("rejection" in str(h) for h in hard)
    ):
        min_struct_eff = 1
    if struct_n < min_struct_eff and not (
        has_div and direction == "short" and min_struct_eff <= 1
    ):
        return GateResult(
            False,
            "delivery_confluence_low",
            f"Structural hard {struct_n} < {min_struct_eff} (confluence gate)",
        )
    return None


def check_exhaustion_fade(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
) -> GateResult | None:
    phase = str(lifecycle.get("phase") or "")
    if direction != "short" or phase not in _FADE_PHASES_SHORT:
        return None
    dl = delivery_thresholds(symbol)
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    p_win = _setup_p_win(setup)
    exh_min = float(dl.get("min_p_win_exhaustion", 0.52))
    structure_primary = bool(setup.get("anticipation")) or bool(setup.get("ev_primary")) or any(
        h in hard
        for h in (
            "distribution_structure_confirm",
            "bos_retest_short",
            "prokol_reclaim_short",
            "peak_fade_confirm",
            "pre_dump_div_confirm",
        )
    )
    if structure_primary:
        exh_min = max(
            float(dl.get("min_p_win_forming", 0.35)),
            float(dl.get("min_p_win_anticipation", 0.42)),
        )
    tf = row.get("timeframes") or {}
    has_div = bool(
        (tf.get("1h_closed") or tf.get("1h") or {}).get("bearish_rsi_div")
        or (tf.get("4h_closed") or tf.get("4h") or {}).get("bearish_rsi_div")
    )
    closed_break = any("close_below_support" in h for h in hard)
    adx_max = float(dl.get("exhaustion_adx_max", 32.0))
    adx_raw = (tf.get("1h_closed") or tf.get("1h") or {}).get("adx14")
    if adx_raw is None:
        return GateResult(
            False,
            "data_missing_adx1h",
            "Fade-at-top: ADX1h отсутствует — нет данных для gate",
        )
    adx14 = float(adx_raw)
    if adx14 > adx_max and not has_div and not closed_break and not structure_primary:
        return GateResult(
            False,
            "exhaustion_strong_trend",
            f"Fade при ADX1h {adx14:.0f} > {adx_max:.0f} — сильный тренд, жди div/break",
        )
    if p_win is not None and p_win < exh_min and not has_div and not closed_break:
        return GateResult(
            False,
            "exhaustion_fade_weak",
            f"Fade-at-top P(win) {p_win:.2f} < {exh_min:.2f} без div/closed break",
        )
    return None


def check_lifecycle_chg24_sanity(
    setup: dict[str, Any],
    *,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
) -> GateResult | None:
    """MLIVE-8: block impulse long on deep negative 24h tape (knife-catch)."""
    phase = str(lifecycle.get("phase") or "")
    if direction != "long" or phase != "impulse_initiating":
        return None
    try:
        chg = float(row.get("chg_24h_pct") or row.get("change_24h_pct") or 0)
    except (TypeError, ValueError):
        chg = 0.0
    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    event = str(struct.get("event") or struct.get("bos_choch") or "").lower()
    choch = bool(struct.get("choch_detected")) or "choch" in event
    if chg <= -15.0 and not choch:
        return GateResult(
            False,
            "impulse_knife_catch",
            f"Long impulse при {chg:.1f}%/24h без CHoCH — knife-catch",
        )
    return None


def check_impulse_long(
    setup: dict[str, Any],
    *,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    symbol: str,
) -> GateResult | None:
    phase = str(lifecycle.get("phase") or "")
    if phase not in _PUMP_PHASES_LONG:
        return None
    dl = delivery_thresholds(symbol)
    sess = row.get("session") or {}
    pos_raw = sess.get("pos_in_range")
    if pos_raw is None:
        return GateResult(
            False,
            "data_missing_pos_in_range",
            "Лонг-импульс: pos_in_range отсутствует — нет session данных",
        )
    pos = float(pos_raw)
    min_pos = float(dl.get("impulse_long_min_pos", 0.52))
    hi = float(sess.get("high_24h") or 0)
    lo = float(sess.get("low_24h") or 0)
    px = float(row.get("price") or 0)
    need_mid = bool(dl.get("impulse_long_above_mid", True))
    mid = (hi + lo) / 2.0 if hi > lo else 0.0
    if pos < min_pos:
        return GateResult(
            False,
            "impulse_session_weak",
            f"Лонг-импульс: pos_in_range {pos:.2f} < {min_pos:.2f} — нет session momentum",
        )
    if need_mid and mid > 0 and px > 0 and px < mid:
        return GateResult(
            False,
            "impulse_session_weak",
            f"Цена {px:.4g} ниже mid 24h {mid:.4g} — слабый импульс сессии",
        )
    market = row.get("market") or {}
    oi_chg = market.get("oi_chg_1h")
    min_oi = float(dl.get("impulse_long_min_oi_chg_1h", 0.005))
    if oi_chg is not None:
        try:
            oi_f = float(oi_chg)
        except (TypeError, ValueError):
            oi_f = 0.0
        if oi_f < min_oi:
            return GateResult(
                False,
                "impulse_oi_weak",
                f"OI 1h Δ {oi_f * 100:.2f}% < {min_oi * 100:.1f}% — нет притока позиций",
            )
    _ = setup, symbol
    return None


def _row_volume_ratio20(row: dict[str, Any]) -> float | None:
    tf = row.get("timeframes") or {}
    for key in ("15m_closed", "15m", "5m_closed", "1m_closed"):
        block = tf.get(key) or {}
        if not isinstance(block, dict):
            continue
        raw = block.get("vol_ratio")
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def check_meme_pump_volume_ratio(
    setup: dict[str, Any],
    *,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    symbol: str,
) -> GateResult | None:
    """Meme pump phases: volume_ratio20 hard floor (signature vs extreme)."""
    if direction != "long":
        return None
    phase = str(lifecycle.get("phase") or "")
    setup_type = str(setup.get("setup_type") or setup.get("phase") or "")
    if phase not in PUMP_PHASES_LONG and setup_type not in {
        "pump_signature",
        "pump_extreme",
        "cex_pump",
    }:
        return None
    vol_ratio = _row_volume_ratio20(row)
    if vol_ratio is None:
        return GateResult(
            False,
            "data_missing_volume_ratio20",
            "Meme pump: volume_ratio20 отсутствует",
        )
    mp = universal_section("gate").get("meme_pump") or {}
    if not isinstance(mp, dict):
        mp = {}
    sig_min = float(mp.get("volume_ratio_pump_signature_min", 3.0))
    extreme_min = float(mp.get("volume_ratio_pump_extreme_min", 5.0))
    sess = row.get("session") or {}
    chg24 = abs(
        float(
            row.get("chg_24h_pct")
            or row.get("change_24h_pct")
            or sess.get("change_24h_pct")
            or 0
        )
    )
    pump_extreme_pct = float(universal_section("scanner").get("pump_extreme_pct", 15.0))
    min_required = extreme_min if (
        setup_type == "pump_extreme"
        or phase == "mega_leg_continuation"
        or chg24 >= pump_extreme_pct
    ) else sig_min
    if vol_ratio < min_required:
        tier = "pump_extreme" if min_required >= extreme_min else "pump_signature"
        return GateResult(
            False,
            "meme_pump_volume_low",
            f"{tier}: volume_ratio20 {vol_ratio:.1f} < {min_required:.1f}",
        )
    _ = symbol
    return None


def check_accumulation_long(
    setup: dict[str, Any],
    *,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    symbol: str,
) -> GateResult | None:
    phase = str(lifecycle.get("phase") or "")
    if phase != "accumulation":
        return None
    dl = delivery_thresholds(symbol)
    acc_min = float(dl.get("min_p_win_accumulation", 0.48))
    p_win = _setup_p_win(setup)
    chg24 = float(setup.get("context_chg_24h_pct") or row.get("chg_24h_pct") or 0)
    if p_win is not None and p_win < acc_min and chg24 < -8.0:
        # Mission: catch the start of accumulation. Strong professional-map accumulation
        # (VP coil + bid absorption + thin asks + bullish CVD + rising OI) overrides the
        # weak-P(win) block so we don't filter out the very setups we exist to find.
        market = row.get("market") if isinstance(row.get("market"), dict) else {}
        try:
            acc_score = float(market.get("map_accumulation_score") or 0)
        except (TypeError, ValueError):
            acc_score = 0.0
        if acc_score >= 0.6:
            return None
        return GateResult(
            False,
            "accumulation_long_weak",
            f"Weak accumulation P(win) {p_win:.2f} < {acc_min:.2f} при chg24 {chg24:.1f}%",
        )
    _ = symbol
    return None


def run_quality_gates(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
) -> list[GateResult]:
    """All phase-quality checks (ex-_delivery_quality_gate)."""
    out: list[GateResult] = []
    for fn in (
        lambda: check_delivery_confluence(
            setup,
            direction=direction,
            symbol=symbol,
            lifecycle=lifecycle,
            row=row,
        ),
        lambda: check_exhaustion_fade(
            setup,
            direction=direction,
            symbol=symbol,
            lifecycle=lifecycle,
            row=row,
        ),
        lambda: check_impulse_long(
            setup, lifecycle=lifecycle, row=row, symbol=symbol
        ),
        lambda: check_accumulation_long(
            setup, lifecycle=lifecycle, row=row, symbol=symbol
        ),
    ):
        hit = fn()
        if hit is not None:
            out.append(hit)
    return out


__all__ = [
    "check_accumulation_long",
    "check_delivery_confluence",
    "check_exhaustion_fade",
    "check_impulse_long",
    "check_lifecycle_chg24_sanity",
    "check_meme_pump_volume_ratio",
    "passes_meme_anomaly_gate",
    "run_quality_gates",
    "_row_chg24_abs",
    "_row_rng24",
]
