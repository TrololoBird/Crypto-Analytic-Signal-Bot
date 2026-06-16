"""Delivery gates — freshness, tier, explain, wash, pipeline."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from hunt_core.analysis.adx_thresholds import ADX_STRONG_MIN, ADX_TREND_MIN

if TYPE_CHECKING:
    from hunt_core.deliver.dispatch import SniperConfig
from hunt_core.regime.leg_fsm import blocks_premature_exhaustion_short
from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.gate.policy import direction_block_reason
from hunt_core.params.store import (
    delivery_thresholds,
    effective_hunt_params,
    filter_thresholds,
    phase_matrix_thresholds,
)
from hunt_core.paths import SIGNAL_STATE
from hunt_core.track.events import record_funnel_stage
from hunt_core.track.outcomes import entry_lifecycle_phase, outcome_kind
from hunt_core.track.prep_shadow import prep_shadow_delivery_fuel_adjustment
from hunt_core.track.tracker import load_tracker_state

LOG = logging.getLogger(__name__)

_WASH_CALIBRATION_DEFAULTS: dict[str, float] = {
    "wash_z_threshold": 4.0,
    "wti_threshold": 0.65,
    "max_velocity_z": 4.5,
}
# Fail-closed when calibration cannot load: lower cuts block more aggressively.
_WASH_CALIBRATION_FAIL_CLOSED: dict[str, float] = {
    "wash_z_threshold": 3.0,
    "wti_threshold": 0.50,
    "max_velocity_z": 3.5,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _has_real_quote_vol_baseline(*, market: dict[str, Any], row: dict[str, Any]) -> bool:
    for source in (market, row):
        raw = source.get("quote_vol_baseline")
        if raw is None:
            continue
        baseline = _optional_float(raw)
        if baseline is not None and baseline > 0.0:
            return True
    return False


def _quote_volume_fields_present(
    *, market: dict[str, Any], row: dict[str, Any], tf15: dict[str, Any]
) -> bool:
    """True when row carries explicit volume fields (live path), vs absent (replay JSONL)."""
    for source in (row, market, tf15):
        for key in ("quote_volume", "quote_volume_24h"):
            if source.get(key) is not None:
                return True
    return False


def wash_volume_z_score(
    *,
    quote_volume: float,
    baseline_volume: float,
    sigma: float = 0.0,
) -> float:
    """Rolling Z on quote volume (A8/A9: wash when vol >> baseline)."""
    if baseline_volume <= 0:
        return 0.0
    if sigma > 0:
        return (quote_volume - baseline_volume) / sigma
    # Fallback: ratio-based pseudo-Z when σ unknown
    ratio = quote_volume / baseline_volume
    if ratio <= 1.0:
        return 0.0
    return min(6.0, math.log(ratio) * 2.0)


def wash_trading_index(
    *,
    price_change_pct: float,
    volume_z: float,
) -> float:
    """WTI-style index (A10): high vol Z with flat/small price move → wash."""
    if abs(price_change_pct) >= 3.0:
        return 0.0
    if volume_z < 2.0:
        return 0.0
    return round(min(1.0, volume_z / 6.0), 3)


def pump_dump_stage(
    *,
    change_24h_pct: float,
    pos_in_range: float | None,
    volume_z: float,
) -> str | None:
    """Sequential P&D stage label (A9/MSS). Returns None if no pattern."""
    pos = pos_in_range if pos_in_range is not None else 0.5
    if change_24h_pct >= 15.0 and volume_z >= 2.0 and pos >= 0.75:
        return "pump_peak"
    if change_24h_pct >= 8.0 and volume_z >= 1.5 and pos >= 0.65:
        return "pump_active"
    if change_24h_pct <= -8.0 and pos <= 0.35 and volume_z >= 1.0:
        return "dump_active"
    if change_24h_pct <= -15.0 and pos <= 0.25:
        return "dump_exhaustion"
    return None


def kinematic_z(
    *,
    change_1h_pct: float,
    change_24h_pct: float,
) -> tuple[float, float]:
    """Velocity and acceleration Z proxies (A10 shield-regime)."""
    velocity = change_1h_pct
    # Acceleration: 1h move vs scaled 24h pace
    expected_1h = change_24h_pct / 24.0
    acceleration = velocity - expected_1h
    # Normalize with soft bounds
    v_z = max(-6.0, min(6.0, velocity / 3.0))
    a_z = max(-6.0, min(6.0, acceleration / 2.0))
    return round(v_z, 2), round(a_z, 2)


def _wash_calibrated_thresholds() -> dict[str, float]:
    """Load calibrated wash/kinematic cuts from hunt_calibration.json (H0 loop)."""
    defaults = _WASH_CALIBRATION_DEFAULTS
    try:
        from hunt_core.params.store import load_calibration  # noqa: PLC0415

        cal = load_calibration()
        wk = (cal.get("outcome_calibration") or {}).get("wash_kinematic") or {}
        if not isinstance(wk, dict):
            LOG.warning(
                "wash calibration invalid type=%s; using fail-closed thresholds",
                type(wk).__name__,
            )
            return dict(_WASH_CALIBRATION_FAIL_CLOSED)
        return {
            "wash_z_threshold": float(wk.get("wash_z_threshold", defaults["wash_z_threshold"])),
            "wti_threshold": float(wk.get("wti_threshold", defaults["wti_threshold"])),
            "max_velocity_z": float(wk.get("max_velocity_z", defaults["max_velocity_z"])),
        }
    except Exception:
        LOG.exception("wash calibration load failed; using fail-closed thresholds")
        return dict(_WASH_CALIBRATION_FAIL_CLOSED)


def wash_block_reason(
    *,
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    wash_z_threshold: float | None = None,
    wti_threshold: float | None = None,
) -> str | None:
    """Return gate block code or None. Blocks suspicious wash before delivery."""
    cuts = _wash_calibrated_thresholds()
    wash_z_threshold = float(wash_z_threshold if wash_z_threshold is not None else cuts["wash_z_threshold"])
    wti_threshold = float(wti_threshold if wti_threshold is not None else cuts["wti_threshold"])
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    tf15 = (row.get("timeframes") or {}).get("15m") or {}
    quote_vol = _safe_float(
        row.get("quote_volume")
        or market.get("quote_volume_24h")
        or tf15.get("quote_volume")
    )
    baseline_real = _has_real_quote_vol_baseline(market=market, row=row)
    if not baseline_real and quote_vol == 0.0:
        if _quote_volume_fields_present(market=market, row=row, tf15=tf15):
            return "wash_data_missing"
        return None

    baseline = _safe_float(
        market.get("quote_vol_baseline")
        or row.get("quote_vol_baseline"),
        quote_vol * 0.3,
    )
    if baseline <= 0:
        baseline = max(quote_vol * 0.25, 1_000_000.0)

    chg_24h = _safe_float(row.get("chg_24h_pct") or row.get("change_24h_pct"))
    vol_z = wash_volume_z_score(
        quote_volume=quote_vol,
        baseline_volume=baseline,
    )
    wti = wash_trading_index(price_change_pct=chg_24h, volume_z=vol_z)

    if vol_z >= wash_z_threshold and abs(chg_24h) < 2.0:
        return "wash_trading"
    if wti >= wti_threshold:
        return "wash_trading_wti"

    # Pre-flagged from microstructure enrichment
    ms = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
    if ms.get("wash_flag") or ms.get("manipulation_wash"):
        return "wash_trading_ms"

    _ = lifecycle  # reserved for phase-aware thresholds
    return None


def _candle_fields(block: dict[str, Any]) -> tuple[float, float, float, float]:
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    o = _safe_float(candle.get("open") or block.get("open"))
    h = _safe_float(candle.get("high") or block.get("high"))
    l = _safe_float(candle.get("low") or block.get("low"))
    c = _safe_float(candle.get("close") or block.get("close"))
    return o, h, l, c


def _wick_body_ratio(open_: float, high: float, low: float, close: float) -> float:
    """Body share of full range — low ratio = wick-dominated trap bar."""
    rng = high - low
    if rng <= 0:
        return 1.0
    body = abs(close - open_)
    return body / rng


def detect_prokol(
    *,
    level: float,
    break_direction: str,
    tf: dict[str, Any] | None = None,
    break_pct: float = 0.005,
    return_bars: int = 2,
) -> dict[str, Any]:
    """Prokol/trap: broke level >0.5% then reclaimed within 1–2 closed bars (Phase 4B).

    ``break_direction`` is the trapped side: ``long`` = false upside break,
    ``short`` = false downside break.
    """
    out: dict[str, Any] = {
        "prokol": False,
        "trap_direction": break_direction,
        "tf_trap": False,
        "break_pct": break_pct,
    }
    if level <= 0 or not tf:
        return out

    d = break_direction.lower().strip()
    blocks: list[dict[str, Any]] = []
    for key in ("5m_closed", "15m_closed", "1h_closed"):
        block = tf.get(key)
        if isinstance(block, dict) and block.get("closed_bar"):
            blocks.append(block)
    if len(blocks) < 2:
        return out

    recent = blocks[-min(len(blocks), return_bars + 1) :]
    broke_idx = -1
    for i, block in enumerate(recent):
        _o, hi, lo, close = _candle_fields(block)
        if d == "long" and hi > level * (1.0 + break_pct):
            broke_idx = i
            break
        if d == "short" and lo < level * (1.0 - break_pct):
            broke_idx = i
            break
    if broke_idx < 0:
        return out

    reclaimed = False
    for block in recent[broke_idx + 1 : broke_idx + 1 + return_bars]:
        _o, _hi, _lo, close = _candle_fields(block)
        if d == "long" and close <= level:
            reclaimed = True
            break
        if d == "short" and close >= level:
            reclaimed = True
            break
    if not reclaimed:
        return out

    out["prokol"] = True
    r1h = tf.get("1h_closed") or tf.get("1h") or {}
    if isinstance(r1h, dict):
        o, hi, lo, c = _candle_fields(r1h)
        if hi > lo and _wick_body_ratio(o, hi, lo, c) < 0.30:
            out["tf_trap"] = True
    return out


# Short phases where high velocity IS the signal, not a late chase: fading a
# vertical peak (price still ripping up) and riding an active dump (price falling
# fast). The manual trader wants these — gating them out misses the move.
_KINEMATIC_EXEMPT_SHORT_PHASES = frozenset(
    {"exhaustion_at_high", "distribution", "dump_active"}
)
_KINEMATIC_EXEMPT_LONG_PHASES = frozenset(
    {"post_dump_bounce", "recovery", "accumulation"}
)


def kinematic_block_reason(
    *,
    row: dict[str, Any],
    direction: str = "",
    lifecycle_phase: str = "",
    max_velocity_z: float | None = None,
) -> str | None:
    """Block late chase when 1h velocity Z is extreme (A10).

    Direction/phase-aware: a short fading the peak or riding a dump treats high
    velocity as the entry trigger, so those phases are exempt. The chase guard
    still applies to longs and to non-fade shorts.
    """
    if direction.lower().strip() == "short" and str(lifecycle_phase) in _KINEMATIC_EXEMPT_SHORT_PHASES:
        return None
    cuts = _wash_calibrated_thresholds()
    max_velocity_z = float(
        max_velocity_z if max_velocity_z is not None else cuts["max_velocity_z"]
    )
    tf1h = (row.get("timeframes") or {}).get("1h") or {}
    chg_1h_raw = _optional_float(tf1h.get("change_pct") or tf1h.get("price_change_pct"))
    chg_24h_raw = _optional_float(row.get("chg_24h_pct") or row.get("change_24h_pct"))
    if chg_1h_raw is None and chg_24h_raw is None:
        return "kinematic_data_missing"
    chg_1h = chg_1h_raw if chg_1h_raw is not None else 0.0
    chg_24h = chg_24h_raw if chg_24h_raw is not None else 0.0
    v_z, _ = kinematic_z(change_1h_pct=chg_1h, change_24h_pct=chg_24h)
    phase = str(lifecycle_phase)
    if direction.lower().strip() == "long" and phase in _KINEMATIC_EXEMPT_LONG_PHASES:
        if v_z >= max_velocity_z:
            return "kinematic_chase"
        return None
    if abs(v_z) >= max_velocity_z:
        return "kinematic_chase"
    return None


MIN_QUOTE_VOL_24H_USD = 1_000_000.0
MIN_OPEN_INTEREST_USD = 100_000.0


def liquidity_skip_reason(
    *,
    quote_volume: float,
    oi: float | None,
    last_price: float,
    symbol: str = "",
) -> str | None:
    """Return error tag when symbol is too illiquid for reliable signals."""
    sym = symbol.upper()
    if sym in PINNED_SYMBOLS:
        return None
    if float(quote_volume or 0) < MIN_QUOTE_VOL_24H_USD:
        return f"liquidity_low_vol24h:{quote_volume:.0f}"
    if oi is not None and last_price > 0:
        oi_usd = float(oi) * last_price
        if oi_usd < MIN_OPEN_INTEREST_USD:
            return f"liquidity_low_oi:{oi_usd:.0f}"
    return None



def _vwap_extreme_atr(symbol: str = "") -> float:
    flt = filter_thresholds(symbol)
    return float(flt.get("vwap_extreme_atr", 2.25))
from hunt_core.data.universe import PINNED_SYMBOLS

ADX_TREND_BLOCK = ADX_STRONG_MIN  # runtime may override via active_params().adx_trend_block
DI_DOMINANCE = 1.25

_PUMP_PREP_PHASES = frozenset(
    {
        "post_dump_bounce",
        "accumulation",
        "recovery",
        "breakout_arming",
        "impulse_initiating",
    }
)
_FADE_PREP_PHASES = frozenset({"exhaustion_at_high", "distribution"})


def directional_filters(
    tf: dict[str, Any],
    *,
    direction: str,
    pos_in_range: float,
    symbol: str = "",
    lifecycle_phase: str = "",
    fall_from_high_pct: float = 0.0,
    chg_24h_pct: float | None = None,
) -> tuple[float, list[str], list[str]]:
    """Returns (score_delta, soft_triggers, hard_blocks)."""
    r1h = tf.get("1h") or {}
    r15 = tf.get("15m_closed") or tf.get("15m") or {}
    adx = float(r1h.get("adx14") or 0.0)
    plus = float(r1h.get("plus_di") or 0.0)
    minus = float(r1h.get("minus_di") or 0.0)
    st_dir = r1h.get("supertrend_dir")
    vdev = r15.get("vwap_dev_atr")
    obv_rising = r1h.get("obv_rising")

    delta = 0.0
    triggers: list[str] = []
    blocks: list[str] = []
    adx_block = effective_hunt_params(symbol).adx_trend_block
    vwap_extreme = _vwap_extreme_atr(symbol)
    phase = str(lifecycle_phase or "")
    mid_dump = phase == "dump_active" and fall_from_high_pct >= 12.0
    short_prep = phase in _FADE_PREP_PHASES
    pump_prep = phase in _PUMP_PREP_PHASES

    if direction == "short":
        if adx >= ADX_TREND_MIN and plus > 0 and plus > minus * DI_DOMINANCE:
            if mid_dump:
                delta -= 8.0
                triggers.append(f"adx_uptrend_mid_dump_soft_{adx:.0f}")
            elif short_prep:
                delta -= 8.0
                triggers.append(f"adx_uptrend_fade_prep_soft_{adx:.0f}")
            elif adx >= adx_block:
                blocks.append(f"adx1h_uptrend_{adx:.0f}")
            else:
                delta -= 15.0
                triggers.append("adx1h_uptrend_against_short")
        if st_dir == 1:
            delta -= 8.0
            triggers.append("headwind_supertrend_1h_up")
        if vdev is not None and float(vdev) <= -vwap_extreme:
            vdev_f = float(vdev)
            dump_leg = (
                mid_dump
                or phase in {"dump_active", "distribution"}
                or (phase == "impulse_initiating" and fall_from_high_pct >= 8.0)
            )
            if dump_leg:
                delta -= 5.0
                triggers.append(f"vwap_oversold_dump_leg_soft_{vdev_f:.2f}atr")
            elif short_prep:
                delta -= 8.0
                triggers.append(f"vwap_oversold_fade_prep_soft_{vdev_f:.2f}atr")
            else:
                blocks.append(f"vwap_oversold_{vdev_f:.2f}atr")
        if obv_rising is False and pos_in_range >= 0.70:
            delta += 8.0
            triggers.append("obv_distribution_at_top")
    else:
        if adx >= ADX_TREND_MIN and minus > 0 and minus > plus * DI_DOMINANCE:
            if pump_prep:
                delta -= 8.0
                triggers.append(f"adx_downtrend_pump_prep_soft_{adx:.0f}")
            elif adx >= adx_block:
                blocks.append(f"adx1h_downtrend_{adx:.0f}")
            else:
                delta -= 15.0
                triggers.append("adx1h_downtrend_against_long")
        if st_dir == -1:
            delta -= 8.0
            triggers.append("headwind_supertrend_1h_down")
        if vdev is not None and float(vdev) >= vwap_extreme:
            if pump_prep and pos_in_range <= 0.45:
                delta -= 5.0
                triggers.append(f"vwap_stretched_pump_prep_soft_{float(vdev):.2f}atr")
            else:
                blocks.append(f"vwap_overbought_{float(vdev):.2f}atr")
        if (
            phase == "accumulation"
            and chg_24h_pct is not None
            and float(chg_24h_pct) < -8.0
            and pos_in_range < 0.45
        ):
            delta -= 15.0
            triggers.append(
                f"weak_accumulation_soft_chg{float(chg_24h_pct):.0f}_pos{pos_in_range:.2f}"
            )
        if obv_rising is True and pos_in_range <= 0.30:
            delta += 8.0
            triggers.append("obv_accumulation_at_low")
    return delta, triggers, blocks




DEFAULT_MIN_SAMPLES = 12
DEFAULT_MAX_WR = 0.28
DEFAULT_PRIOR_WR = 0.35


@dataclass(frozen=True, slots=True)
class PhaseStats:
    phase: str
    direction: str
    wins: int
    losses: int

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else 0.0


def _labeled_outcomes(signals: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        reason = str(sig.get("close_reason") or "unknown")
        pnl = sig.get("pnl_pct")
        if pnl is None:
            continue
        phase = entry_lifecycle_phase(sig)
        direction = str(sig.get("direction") or "")
        if not phase or phase == "?" or direction not in {"long", "short"}:
            continue
        kind = outcome_kind(reason, pnl_pct=float(pnl))
        if kind not in {"win", "loss"}:
            continue
        rows.append((phase, direction, "win" if kind == "win" else "loss"))
    return rows


def _rebuild_cache() -> dict[tuple[str, str], PhaseStats]:
    state = load_tracker_state()
    signals = state.get("signals") or {}
    buckets: dict[tuple[str, str], list[str]] = {}
    for phase, direction, kind in _labeled_outcomes(signals):
        buckets.setdefault((phase, direction), []).append(kind)

    disabled: dict[tuple[str, str], PhaseStats] = {}
    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_wr = float(pm.get("max_wr", DEFAULT_MAX_WR))
    prior_wr = float(pm.get("prior_wr", DEFAULT_PRIOR_WR))
    for (phase, direction), kinds in buckets.items():
        wins = sum(1 for k in kinds if k == "win")
        losses = sum(1 for k in kinds if k == "loss")
        stats = PhaseStats(phase=phase, direction=direction, wins=wins, losses=losses)
        n0 = 4.0
        adj_wr = (wins + prior_wr * n0) / (stats.n + n0) if stats.n else prior_wr
        if stats.n >= min_n and adj_wr < max_wr:
            disabled[(phase, direction)] = stats
    return disabled


def disabled_phase_pairs(
    *,
    force: bool = False,
    state: Any | None = None,
) -> dict[tuple[str, str], PhaseStats]:
    from hunt_core.runtime.state import current_symbol_state  # noqa: PLC0415

    store = state or current_symbol_state()
    mtime = SIGNAL_STATE.stat().st_mtime if SIGNAL_STATE.is_file() else 0.0
    if force or mtime != store.phase_matrix_mtime:
        store.phase_matrix_disabled = _rebuild_cache()
        store.phase_matrix_mtime = mtime
    return dict(store.phase_matrix_disabled)


def phase_matrix_gate(
    phase: str,
    direction: str,
    *,
    state: Any | None = None,
) -> tuple[bool, str]:
    """Return (blocked, human reason). Empty phase → not blocked."""
    if not phase or phase == "no_setup":
        return False, ""
    key = (phase, str(direction))
    stats = disabled_phase_pairs(state=state).get(key)
    if stats is None:
        return False, ""
    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_wr = float(pm.get("max_wr", DEFAULT_MAX_WR))
    return (
        True,
        f"Phase {phase} {direction}: WR {stats.wr * 100:.0f}% на n={stats.n} "
        f"(порог {max_wr * 100:.0f}%, min n={min_n}) — auto-off",
    )





def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def entry_chase_tol() -> float:
    return _env_float("HUNT_ENTRY_CHASE_TOL", 0.002)


def max_tp1_progress() -> float:
    """Max fraction of entry→TP1 move already captured before TG ships."""
    return _env_float("HUNT_MAX_TP1_PROGRESS", 0.25)


def price_in_entry_zone(
    setup: dict[str, Any],
    price: float,
    *,
    direction: str,
    tol: float | None = None,
) -> bool:
    """True when price is inside the latched entry band (fade-at-top / in-zone dump)."""
    if price <= 0:
        return False
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return False
    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return False
    band_tol = entry_chase_tol() if tol is None else tol
    if direction == "short":
        return zone_lo * (1.0 - band_tol) <= price <= zone_hi * (1.0 + band_tol)
    if direction == "long":
        return zone_lo * (1.0 - band_tol) <= price <= zone_hi * (1.0 + band_tol)
    return False


def delivery_freshness_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    chase_tol: float | None = None,
    max_progress: float | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> str | None:
    """Return block code when price is too late for a new manual entry, else None."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return "delivery_bad_price"

    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return "delivery_bad_entry_geometry"

    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return "delivery_bad_entry_geometry"

    tol = entry_chase_tol() if chase_tol is None else chase_tol
    tp1 = float(setup.get("tp1") or 0)
    _ = max_progress  # tp1_progress demotion handled in dispatch + delivery_tier

    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    phase = str(lc_dict.get("phase") or "")
    fall = float(lc_dict.get("fall_from_high_pct") or 0)

    if direction == "short" and phase == "dump_active" and fall >= 40.0:
        if price < zone_lo or price > zone_hi * (1.0 + tol):
            return "delivery_dump_entry_stale"
    elif direction == "short":
        if tp1 > 0 and price <= tp1:
            return "delivery_past_tp1"
        if price < zone_lo * (1.0 - tol):
            return "delivery_late_chase"
    elif direction == "long":
        if tp1 > 0 and price >= tp1:
            return "delivery_past_tp1"
        if price > zone_hi * (1.0 + tol):
            return "delivery_late_chase"
    return None


DeliveryTier = Literal["armed", "triggered"]


def classify_delivery_tier(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
) -> DeliveryTier | None:
    """Return tier when setup may ship; None when delivery is stale or monitor-only."""

    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    from hunt_core.scan.routing import resolve_delivery_mode  # noqa: PLC0415

    mode = resolve_delivery_mode(lc_dict, setup)

    if mode == "monitor_only" and not setup.get("confirmed"):
        return None

    if delivery_hard_block(direction=direction, setup=setup, row=row):
        return None
    price = float(row.get("price") or 0)
    if price <= 0:
        return None

    if mode == "armed_first" and not setup.get("confirmed"):
        return "armed"

    if price_in_entry_zone(setup, price, direction=direction):
        return "triggered"
    return "armed"


def delivery_hard_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
) -> str | None:
    """Hard stale blocks only — never use for ARMED/TRIGGERED routing."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return "delivery_bad_price"

    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return "delivery_bad_entry_geometry"

    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return "delivery_bad_entry_geometry"

    tp1 = float(setup.get("tp1") or 0)

    if direction == "short":
        if tp1 > 0 and price <= tp1:
            return "delivery_past_tp1"
    elif direction == "long":
        if tp1 > 0 and price >= tp1:
            return "delivery_past_tp1"

    return None


def tp1_progress_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    max_progress: float | None = None,
) -> str | None:
    """Optional cap for TRIGGERED tier only (not applied to ARMED)."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return None
    tp1 = float(setup.get("tp1") or 0)
    progress_cap = max_tp1_progress() if max_progress is None else max_progress
    tol = entry_chase_tol()

    if direction == "short":
        if tp1 > 0 and zone_lo > tp1:
            total = zone_lo - tp1
            captured = zone_lo - price
            if total > 0 and captured / total > progress_cap:
                return "delivery_tp1_progress"
        if price < zone_lo * (1.0 - tol):
            return None  # ARMED path — not a hard block
    elif direction == "long":
        if tp1 > 0 and tp1 > zone_hi:
            total = tp1 - zone_hi
            captured = price - zone_hi
            if total > 0 and captured / total > progress_cap:
                return "delivery_tp1_progress"
    return None





BOUNCE_MIN_RISK_REWARD = 0.5
PINNED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"})


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    code: str
    message: str


# Display order for /signals (most actionable first — independent of alert short-circuit).
REPORT_BLOCK_PRIORITY: dict[str, int] = {
    "stale_no_setup": 0,
    "invalidate_short": 1,
    "bias_conflict": 2,
    "short_entry_not_ok": 3,
    "long_blocked_mid_dump": 4,
    "long_below_resistance": 5,
    "long_below_hunt_high": 5,
    "lifecycle_veto_hard": 6,
    "below_forming_min": 7,
    "phase_matrix_disable": 8,
    "premature_exhaustion": 9,
    "not_confirmed": 10,
    "filter_block": 11,
    "not_anomaly": 12,
    "levels_veto": 13,
    "rr_below_min": 14,
    "tp2_too_close": 15,
    "delivery_fuel_low": 16,
    "delivery_confluence_low": 17,
    "data_missing_adx1h": 6,
    "data_missing_pos_in_range": 6,
    "exhaustion_fade_weak": 18,
    "accumulation_long_weak": 19,
    "exhaustion_strong_trend": 18,
    "impulse_session_weak": 19,
    "impulse_oi_weak": 19,
    "prep_shadow_tighten": 16,
}


def _lifecycle_dict(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    return {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "short_entry_ok": lifecycle.short_entry_ok,
        "short_confirm_ok": lifecycle.short_confirm_ok,
        "invalidate_short": lifecycle.invalidate_short,
    }


def _min_rr(symbol: str, direction: str, lc: dict[str, Any]) -> float:
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    if sym in PINNED_SYMBOLS:
        return cal.pinned_min_risk_reward
    phase = str(lc.get("phase") or "")
    if direction == "long" and phase == "post_dump_bounce":
        return BOUNCE_MIN_RISK_REWARD
    return cal.min_risk_reward


def _setup_fuel(setup: dict[str, Any], direction: str) -> float:
    key = "dump_fuel" if direction == "short" else "long_fuel"
    alt = "dump_score" if direction == "short" else "long_score"
    return float(setup.get(key) or setup.get(alt) or 0)


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


def _passes_meme_anomaly_gate(
    *,
    sym: str,
    row: dict[str, Any],
    lc: dict[str, Any],
    cal: Any,
    fuel: float,
) -> bool:
    """Meme hunt volatility floor — waive for high-fuel dump confirms near threshold."""
    if sym in PINNED_SYMBOLS:
        return True
    if bool(row.get("young_listing")):
        return True
    chg24 = _row_chg24_abs(row)
    rng24 = _row_rng24(row)
    min_chg = float(cal.anomaly_min_chg_24h_pct)
    min_rng = float(cal.anomaly_min_range_24h_pct)
    if chg24 >= min_chg or rng24 >= min_rng:
        return True
    fall = float(lc.get("fall_from_high_pct") or 0)
    if fuel >= 130 and (rng24 >= min_rng * 0.95 or fall >= 15.0):
        return True
    if fuel >= 120 and fall >= 18.0:
        return True
    # Confirmed mid-dump: range just under regime floor (PLAY 14.9% vs 15.3%).
    if fuel >= 75 and fall >= 8.0 and rng24 >= min_rng * 0.975:
        return True
    return False


_PUMP_PHASES_LONG = frozenset({"impulse_initiating", "breakout_arming"})
_FADE_PHASES_SHORT = frozenset({"exhaustion_at_high", "distribution"})
# Mid-dump continuation is monitor-only — Telegram ships at dump *start* (fade/top).
_SHORT_DUMP_START_LC_PHASES = frozenset({"exhaustion_at_high", "distribution"})
_DUMP_CONTINUATION_PHASES = frozenset(
    {"dump_active", "distribution", "impulse_initiating"}
)
_STRUCTURAL_DUMP_MARKERS = (
    "close_below_support",
    "below_support",
    "live_below_support",
    "lost_support",
    "bear_cascade",
)


def _structural_dump_hard(hard: list[Any]) -> bool:
    return any(
        any(marker in str(h) for marker in _STRUCTURAL_DUMP_MARKERS) for h in hard
    )


def _short_dump_start_max_fall_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_start_max_fall_pct", 3.0))


def _short_pre_dump_headroom_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_pre_dump_headroom_pct", 5.0))


def _short_dump_first_break_max_fall_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_first_break_max_fall_pct", 5.0))


def _in_pre_dump_window(
    lc: dict[str, Any],
    *,
    symbol: str = "",
    hunt_high: float = 0.0,
    price: float = 0.0,
) -> bool:
    """True at dump *start*: fall ≤3% or price within ~5% headroom to hunt_high."""
    fall = float(lc.get("fall_from_high_pct") or 0)
    start_max = _short_dump_start_max_fall_pct(symbol)
    headroom_max = _short_pre_dump_headroom_pct(symbol)
    if fall <= start_max:
        return True
    if hunt_high > 0 and price > 0:
        headroom = max(0.0, (hunt_high - price) / hunt_high * 100.0)
        return headroom <= headroom_max and fall <= headroom_max
    return fall <= headroom_max


def _short_dump_delivery_too_late(
    lc: dict[str, Any],
    setup: dict[str, Any],
    *,
    symbol: str = "",
) -> GateResult | None:
    """Block Telegram shorts after the dump leg started — only fade/top entries."""
    fall = float(lc.get("fall_from_high_pct") or 0)
    phase = str(lc.get("phase") or "")
    setup_phase = str(setup.get("phase") or "")
    if phase == "dump_active":
        # Exception: dump_continuation_confirm = fresh structural breakdown WITHIN
        # an ongoing dump (new support broken, secondary signals confirmed). This is
        # not a blind mid-dump chase — it's a confirmed continuation with structural
        # and secondary signal alignment (see confirm_dump() in predump.py).
        hard = setup.get("confirm_hard") or []
        if fall >= 70.0:
            if "dump_continuation_confirm" in hard:
                return None
            if _structural_hard_count(hard, direction="short") >= 2 and any(
                str(h).startswith(("5m_", "15m_", "1h_"))
                and "close_below_support" in str(h)
                for h in hard
            ):
                return None
            return GateResult(
                False,
                "dump_late_chase",
                f"Fall {fall:.1f}% ≥70% — поздний chase, нужен fresh structural break",
            )
        if "dump_continuation_confirm" in hard:
            return None
        # Fresh multi-TF breaks (1m+15m close below support) within an active dump —
        # valid continuation even when fall > 50% (BEATUSDT lesson).
        if _structural_hard_count(hard, direction="short") >= 2:
            return None
        return GateResult(
            False,
            "dump_mid_leg",
            f"Дамп уже идёт (fall {fall:.1f}%) — monitor only, без нового TG",
        )
    start_max = _short_dump_start_max_fall_pct(symbol)
    break_max = _short_dump_first_break_max_fall_pct(symbol)
    headroom_max = _short_pre_dump_headroom_pct(symbol)
    if fall <= start_max:
        return None
    if (
        fall <= break_max
        and fall <= headroom_max
        and phase in _SHORT_DUMP_START_LC_PHASES
        and setup_phase
        in {
            "dump_initiating",
            "dump_confirmed",
            "dump_setup_forming",
            "dump_imminent",
        }
    ):
        return None
    return GateResult(
        False,
        "dump_late_entry",
        f"Fall {fall:.1f}% > {start_max:.0f}% — пропустили начало, поздний дамп не шлём",
    )


def _dump_continuation_short_ok(
    setup: dict[str, Any],
    *,
    phase: str,
    lc: dict[str, Any],
    fuel: float,
    cal_min_fuel: float,
) -> bool:
    """Allow confirmed dump continuation: fresh structural break within dump_active.

    Fires when confirm_dump() tagged dump_continuation_confirm, or when fall >= 15%
    with >=2 structural hard factors (1m+pp breaks) — same bar as dump_mid_leg bypass.
    """
    if phase != "dump_active":
        return False
    if fuel < cal_min_fuel:
        return False
    hard = setup.get("confirm_hard") or []
    if "dump_continuation_confirm" in hard:
        return True
    if "dump_fast_confirm" in hard:
        return True
    fall = float(lc.get("fall_from_high_pct") or 0)
    if fall >= 15.0 and _structural_hard_count(hard, direction="short") >= 2:
        if any("close_below_support" in str(h) for h in hard):
            return True
    return False


_DUMP_CONTINUATION_MIN_RR = 1.05
_CONTINUATION_PCT_MIN_RR = 0.85
_CONFIRMED_STRUCTURAL_DUMP_MIN_RR = 1.05
_PRE_DUMP_STRUCTURAL_MIN_RR = 1.15
_DELIVERY_MIN_RR_FLOOR = 1.6
_STRUCTURAL_DUMP_PHASES = frozenset(
    {
        "dump_initiating",
        "dump_confirmed",
        "dump_setup_forming",
        "dump_imminent",
    }
)
_PRE_DUMP_LC_PHASES = frozenset(
    {
        "exhaustion_at_high",
        "distribution",
        "dump_initiating",
    }
)


def _continuation_pct_min_rr(setup: dict[str, Any]) -> float | None:
    mode = str(setup.get("level_mode") or "")
    if "continuation_pct" in mode:
        return _CONTINUATION_PCT_MIN_RR
    tp2_label = str(setup.get("tp2_label") or "")
    if "cont" in tp2_label.lower():
        return _CONTINUATION_PCT_MIN_RR
    return None


def _confirmed_structural_dump_min_rr(
    setup: dict[str, Any],
    lc: dict[str, Any],
) -> float | None:
    if not bool(setup.get("confirmed")):
        return None
    hard = setup.get("confirm_hard") or []
    if not _structural_dump_hard(hard) and _structural_hard_count(hard, direction="short") < 1:
        return None
    phase = str(lc.get("phase") or "")
    if phase in _PRE_DUMP_LC_PHASES:
        return _PRE_DUMP_STRUCTURAL_MIN_RR
    if phase not in _STRUCTURAL_DUMP_PHASES and phase not in _DUMP_CONTINUATION_PHASES:
        return None
    return _CONFIRMED_STRUCTURAL_DUMP_MIN_RR


def effective_min_rr_for_delivery(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any] | None = None,
) -> float:
    """Single min R:R for gate + contract validation on the confirm delivery path."""
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    fuel = _setup_fuel(setup, direction)
    return _effective_min_rr(
        setup,
        direction=direction,
        symbol=sym,
        lc=lc,
        fuel=fuel,
        cal=cal,
    )


def _effective_min_rr(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lc: dict[str, Any],
    fuel: float,
    cal: HuntCalibratedParams,
) -> float:
    base = _min_rr(symbol, direction, lc)
    if direction != "short":
        return max(_DELIVERY_MIN_RR_FLOOR, base)
    cont_floor = _continuation_pct_min_rr(setup)
    if cont_floor is not None:
        return min(base, cont_floor)
    # For confirmed structural dumps (price broke key support on closed bar),
    # allow lower RR floor (1.05) — the structural trigger IS the edge.
    structural_floor = _confirmed_structural_dump_min_rr(setup, lc)
    if structural_floor is not None:
        return min(base, structural_floor)
    # Dump continuation (dump already >15% in progress): allow 1.05 RR floor.
    if _dump_continuation_short_ok(
        setup,
        phase=str(lc.get("phase") or ""),
        lc=lc,
        fuel=fuel,
        cal_min_fuel=cal.confirm_min_score,
    ):
        return min(base, _DUMP_CONTINUATION_MIN_RR)
    return max(_DELIVERY_MIN_RR_FLOOR, base)


def _tp2_room_blocks(
    setup: dict[str, Any],
    *,
    price: float,
    min_room_pct: float,
    min_rr: float,
) -> bool:
    """Block only when TP2 cramped *and* R:R not already satisfied on TP1 path."""
    tp2 = float(setup.get("tp2") or 0)
    if price <= 0 or tp2 <= 0:
        return False
    room = abs(price - tp2) / price * 100.0
    if room >= min_room_pct:
        return False
    rr = setup.get("risk_reward")
    if rr is not None and float(rr) >= min_rr:
        return False
    if _structural_dump_hard(setup.get("confirm_hard") or []) and len(
        setup.get("confirm_hard") or []
    ) >= 2:
        return False
    return True


def _hard_filter_blocks(
    blocks: list[Any],
    *,
    direction: str,
    phase: str,
    fall_from_high_pct: float = 0.0,
) -> list[str]:
    """Phase-aware filter severity. VWAP/ADX trend filters describe *trend
    continuation* — on an initial pump leg (long) or exhaustion fade (short)
    that is the setup itself, not a contra-signal:
    - VELVET +96% leg: vwap_overbought_5.3atr blocked every confirmed long;
    - BEAT 8.37 top: adx1h_uptrend_* blocked 253 confirmed exhaustion shorts.
    Outside those phases the filters stay hard."""
    out: list[str] = []
    for raw in blocks:
        tag = str(raw)
        if direction == "long" and phase in _PUMP_PHASES_LONG and (
            tag.startswith("vwap_overbought") or tag.startswith("adx1h_uptrend")
        ):
            continue
        if direction == "short" and tag.startswith("adx1h_uptrend"):
            if phase in _FADE_PHASES_SHORT or phase in _DUMP_CONTINUATION_PHASES:
                continue
        if direction == "short" and tag.startswith("vwap_oversold"):
            if phase in {"dump_active", "distribution"}:
                continue
            if phase == "impulse_initiating" and fall_from_high_pct >= 8.0:
                continue
        out.append(tag)
    return out


def _structural_hard_count(hard: list[Any], *, direction: str) -> int:
    keys_short = (
        "close_below_support",
        "rejection",
        "cascade",
        "ws_liq",
        "1m_close",
        "5m_close",
        "15m_close",
        "bear_cascade",
        "lost_support",
        "pp_short",
        "dump_continuation_confirm",
    )
    keys_long = (
        "close_above_resistance",
        "bounce",
        "cascade",
        "broke_resistance",
        "5m_close",
        "bull_cascade",
        "ws_taker_buy",
    )
    keys = keys_short if direction == "short" else keys_long
    return sum(1 for h in hard if any(k in str(h) for k in keys))


def _delivery_quality_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any],
    fuel: float,
    row: dict[str, Any],
) -> GateResult | None:
    """High-conviction Telegram delivery — target 70% WR (confluence + fuel floor)."""
    dl = delivery_thresholds(symbol)
    base_min_fuel = float(dl.get("min_fuel", 72.0))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    fuel_adj, adj_reason = prep_shadow_delivery_fuel_adjustment()
    score = float(setup.get("dump_score") or setup.get("dump_fuel") or 0)
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    struct = _structural_dump_hard(hard)
    continuation = (
        direction == "short"
        and bool(setup.get("confirmed"))
        and fall >= 20.0
        and score >= 120.0
        and ("dump_continuation_confirm" in hard or struct)
    )
    phase = str(lifecycle.get("phase") or "")
    waive_prep_bump = (
        direction == "short"
        and bool(setup.get("confirmed"))
        and (
            (
                fuel >= base_min_fuel
                and (struct or "dump_continuation_confirm" in hard or score >= 130.0)
            )
            or (
                phase in {"distribution", "exhaustion_at_high"}
                and fall < 8.0
                and struct
                and fuel >= max(62.0, base_min_fuel - 10.0)
            )
            or (
                continuation
                and fuel >= max(65.0, base_min_fuel - 8.0)
            )
            or (
                score >= 115.0
                and ("dump_continuation_confirm" in hard or struct)
                and fuel >= max(65.0, base_min_fuel - 10.0)
            )
            or (
                score >= 105.0
                and fall >= 50.0
                and ("dump_continuation_confirm" in hard or struct)
                and fuel >= max(62.0, base_min_fuel - 13.0)
            )
        )
    )
    if waive_prep_bump:
        fuel_adj = 0.0
        adj_reason = None
    elif fuel_adj > 0 and score >= 115.0:
        fuel_adj = min(fuel_adj, 1.0)
    min_fuel = max(68.0, base_min_fuel + fuel_adj)
    distribution_fade = (
        phase in {"distribution", "exhaustion_at_high"}
        and fall < 8.0
        and struct
    )
    if distribution_fade:
        min_fuel = min(min_fuel, max(62.0, base_min_fuel - 10.0))
    if continuation and fuel >= 65.0:
        min_fuel = min(min_fuel, 65.0)
    min_struct = int(dl.get("min_structural_hard", 2))
    struct_n = _structural_hard_count(hard, direction=direction)

    if fuel < min_fuel:
        tier_note = f" (adj +{fuel_adj:.0f})" if fuel_adj > 0 else ""
        shadow_note = f" · {adj_reason}" if adj_reason and fuel_adj != 0 else ""
        return GateResult(
            False,
            "delivery_fuel_low" if fuel_adj <= 0 else "prep_shadow_tighten",
            f"Delivery fuel {fuel:.0f} < {min_fuel:.0f}{tier_note} (70% WR tier){shadow_note}",
        )
    min_struct_eff = min_struct
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    start_max = _short_dump_start_max_fall_pct(symbol)
    tf = row.get("timeframes") or {}
    has_div = bool(
        (tf.get("1h") or {}).get("bearish_rsi_div")
        or (tf.get("4h") or {}).get("bearish_rsi_div")
        or (tf.get("1h") or {}).get("bearish_macd_div")
        or (tf.get("4h") or {}).get("bearish_macd_div")
    )
    late_block = _short_dump_delivery_too_late(lifecycle, setup, symbol=symbol)
    if direction == "short" and late_block is not None:
        return late_block
    # Peak fade / dump start: one structural factor is enough near the top (COAI 17:42 lesson).
    if (
        direction == "short"
        and phase in _SHORT_DUMP_START_LC_PHASES
        and fall <= start_max
        and fuel >= min_fuel
        and (
            any("rejection" in str(h) for h in hard)
            or has_div
            or _structural_dump_hard(hard)
        )
    ):
        min_struct_eff = 1
    if (
        direction == "short"
        and phase == "exhaustion_at_high"
        and fuel >= min_fuel
        and any("rejection" in str(h) for h in hard)
    ):
        min_struct_eff = 1
    if (
        direction == "short"
        and "dump_continuation_confirm" in hard
        and fuel >= min_fuel
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

    if direction == "short" and phase in _FADE_PHASES_SHORT:
        exh_min = float(dl.get("exhaustion_short_min_fuel", 78.0))
        tf = row.get("timeframes") or {}
        has_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        closed_break = any("close_below_support" in h for h in hard)
        adx_max = float(dl.get("exhaustion_adx_max", 32.0))
        adx_raw = (tf.get("1h") or {}).get("adx14")
        if adx_raw is None:
            return GateResult(
                False,
                "data_missing_adx1h",
                "Fade-at-top: ADX1h отсутствует — нет данных для gate",
            )
        adx14 = float(adx_raw)
        if adx14 > adx_max and not has_div and not closed_break:
            return GateResult(
                False,
                "exhaustion_strong_trend",
                f"Fade при ADX1h {adx14:.0f} > {adx_max:.0f} — сильный тренд, жди div/break",
            )
        if fuel < exh_min and not has_div and not closed_break:
            return GateResult(
                False,
                "exhaustion_fade_weak",
                f"Fade-at-top fuel {fuel:.0f} < {exh_min:.0f} без div/closed break",
            )

    if direction == "long" and phase in _PUMP_PHASES_LONG:
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

    if direction == "long" and phase == "accumulation":
        acc_min = float(dl.get("accumulation_long_min_fuel", 74.0))
        chg24 = float(setup.get("context_chg_24h_pct") or row.get("chg_24h_pct") or 0)
        if fuel < acc_min and chg24 < -8.0:
            return GateResult(
                False,
                "accumulation_long_weak",
                f"Weak accumulation long fuel {fuel:.0f} < {acc_min:.0f} при chg24 {chg24:.1f}%",
            )

    return None


def _lifecycle_veto_hard(setup: dict[str, Any]) -> GateResult | None:
    for raw in setup.get("confirm_hard") or []:
        tag = str(raw)
        if tag.startswith("veto_lifecycle") or tag.startswith("veto_mtf"):
            label = "mtf_veto_hard" if tag.startswith("veto_mtf") else "lifecycle_veto_hard"
            return GateResult(False, label, f"Confirm veto: {tag}")
    return None


def _bias_conflict(direction: str, lc: dict[str, Any]) -> GateResult | None:
    bias = str(lc.get("recommended_bias") or "")
    if direction == "short" and bias == "long":
        return GateResult(False, "bias_conflict", "Bias long — открытый шорт против lifecycle")
    if direction == "long" and bias == "short":
        return GateResult(False, "bias_conflict", "Bias short — открытый лонг против lifecycle")
    return None


def _core_lifecycle_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    lc: dict[str, Any],
) -> GateResult | None:
    """Shared lifecycle vetoes for delivery and /signals report parity."""
    phase = str(lc.get("phase") or "")
    if phase == "no_setup":
        return GateResult(False, "stale_no_setup", "Lifecycle no_setup — сетап исчез")
    bias_hit = _bias_conflict(direction, lc)
    if bias_hit is not None:
        return bias_hit
    veto = _lifecycle_veto_hard(setup)
    if veto is not None:
        return veto
    return None


def collect_report_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> list[GateResult]:
    """All current blockers for /signals, sorted by operator priority."""

    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    r = row or {}
    fuel = _setup_fuel(setup, direction)
    confirmed = bool(setup.get("confirmed"))
    blockers: list[GateResult] = []

    phase = str(lc.get("phase") or "")
    core = _core_lifecycle_blockers(setup, direction=direction, lc=lc)
    if core is not None:
        blockers.append(core)

    pm_blocked, pm_reason = phase_matrix_gate(phase, direction)
    if pm_blocked:
        blockers.append(GateResult(False, "phase_matrix_disable", pm_reason))

    if direction == "short" and phase == "post_dump_bounce":
        blockers.append(
            GateResult(
                False,
                "short_blocked_bounce",
                "Шорт в post_dump_bounce запрещён — отскок после дампа",
            )
        )

    if direction == "short" and lc.get("invalidate_short"):
        blockers.append(
            GateResult(
                False,
                "invalidate_short",
                "Lifecycle: отскок/пробой вверх — шорт инвалидирован",
            )
        )

    if direction == "short" and not lc.get("short_entry_ok", False):
        if not _dump_continuation_short_ok(
            setup,
            phase=phase,
            lc=lc,
            fuel=fuel,
            cal_min_fuel=cal.confirm_min_score,
        ):
            bias = str(lc.get("recommended_bias") or "—")
            blockers.append(
                GateResult(
                    False,
                    "short_entry_not_ok",
                    f"Lifecycle {phase or '—'} bias={bias} — вход в шорт запрещён",
                )
            )

    if direction == "long":
        fall = float(lc.get("fall_from_high_pct") or 0)
        if phase == "dump_active":
            blockers.append(
                GateResult(
                    False,
                    "long_blocked_mid_dump",
                    "Лонг в mid-dump запрещён — жди post_dump_bounce",
                )
            )
        elif phase not in {
            "post_dump_bounce",
            "impulse_initiating",
            "breakout_arming",
        }:
            hunt_high = float(
                r.get("impulse_high") or ((r.get("impulse") or {}).get("hunt_high")) or 0
            )
            price = float(r.get("price") or 0)
            if hunt_high > 0 and price > 0 and price < hunt_high * 0.90 and fall >= 12.0:
                blockers.append(
                    GateResult(
                        False,
                        "long_below_hunt_high",
                        f"Цена {price:.4g} < 90% hunt_high при fall {fall:.0f}%",
                    )
                )
        res = float(setup.get("resistance_break_level") or 0)
        px = float(r.get("price") or 0)
        r5_close = float((r.get("timeframes") or {}).get("5m_closed", {}).get("close") or 0)
        from hunt_core.scan._confirm_shared import long_resistance_chase_veto  # noqa: PLC0415

        if long_resistance_chase_veto(res, px, r5_close):
            blockers.append(
                GateResult(
                    False,
                    "long_below_resistance",
                    f"Цена {px:.4g} ниже resistance_break {res:.4g} — поздний chase",
                )
            )

    if fuel < cal.forming_min_score:
        blockers.append(
            GateResult(
                False,
                "below_forming_min",
                f"Fuel {fuel:.0f} < forming_min {cal.forming_min_score:.0f}",
            )
        )

    if not confirmed:
        blockers.append(
            GateResult(False, "not_confirmed", "Нет closed-bar confirm (5m/1m)")
        )

    filter_blocks = _hard_filter_blocks(
        setup.get("filter_blocks") or [],
        direction=direction,
        phase=phase,
        fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
    )
    if filter_blocks:
        txt = ", ".join(str(b) for b in filter_blocks)
        blockers.append(GateResult(False, "filter_block", f"Фильтр тренда/VWAP: {txt}"))

    if not _passes_meme_anomaly_gate(sym=sym, row=r, lc=lc, cal=cal, fuel=fuel):
        chg24 = _row_chg24_abs(r)
        rng24 = _row_rng24(r)
        blockers.append(
            GateResult(
                False,
                "not_anomaly",
                f"Не meme-аномалия: chg24={chg24:.1f}% range={rng24:.1f}% "
                f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
            )
        )

    px = float(r.get("price") or 0)
    min_rr = _effective_min_rr(
        setup, direction=direction, symbol=sym, lc=lc, fuel=fuel, cal=cal
    )
    if _tp2_room_blocks(
        setup, price=px, min_room_pct=cal.tp2_min_room_pct, min_rr=min_rr
    ):
        blockers.append(
            GateResult(
                False,
                "tp2_too_close",
                f"TP2 слишком близко ({cal.tp2_min_room_pct:.0f}% room min)",
            )
        )

    if setup.get("levels_viable") is False:
        veto_list = setup.get("levels_veto") or []
        blockers.append(
            GateResult(
                False,
                "levels_veto",
                f"Уровни не viable: {', '.join(str(v) for v in veto_list) or 'структура'}",
            )
        )

    rr = setup.get("risk_reward")
    if rr is not None and float(rr) < min_rr:
        blockers.append(
            GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")
        )

    if direction == "short" and confirmed:
        late = _short_dump_delivery_too_late(lc, setup, symbol=sym)
        if late is not None:
            blockers.append(late)
        session = r.get("session") or {}
        tf = r.get("timeframes") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        blocked, prem_reason = blocks_premature_exhaustion_short(
            phase=phase,
            fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
            bounce_from_low_pct=float(lc.get("bounce_from_low_pct") or 0),
            pos_in_range=float(session.get("pos_in_range") or 0.5),
            has_bear_div=has_bear_div,
            symbol=sym,
        )
        if blocked:
            hard = setup.get("confirm_hard") or []
            if not any("close_below_support" in str(h) for h in hard):
                blockers.append(
                    GateResult(
                        False,
                        "premature_exhaustion",
                        f"Ранний fade-at-top: {prem_reason}",
                    )
                )

    if confirmed:
        delivery_block = _delivery_quality_gate(
            setup,
            direction=direction,
            symbol=sym,
            lifecycle=lc,
            fuel=fuel,
            row=r,
        )
        if delivery_block is not None:
            blockers.append(delivery_block)

    seen: set[str] = set()
    unique: list[GateResult] = []
    for item in blockers:
        if item.code in seen:
            continue
        seen.add(item.code)
        unique.append(item)
    unique.sort(key=lambda g: REPORT_BLOCK_PRIORITY.get(g.code, 50))
    return unique


def primary_block_for_report(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> GateResult:
    """Report parity: same gate stack as live Telegram confirm (evaluate_delivery)."""
    from hunt_core.deliver.dispatch import evaluate_delivery  # noqa: PLC0415

    base_row = dict(row) if isinstance(row, dict) else {}
    gate, tier = evaluate_delivery(
        base_row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        refresh_live_price=False,
    )
    if not gate.ok:
        return gate
    if tier is None:
        return GateResult(False, "delivery_no_tier", "No ARMED/TRIGGERED tier")
    return GateResult(True, "ok", f"tier={tier}")


def evaluate_stale_advice(
    *,
    symbol: str,
    direction: str,
    lifecycle: Any | None,
    setup: dict[str, Any],
    sig: dict[str, Any],
) -> str | None:
    """Action hint for open tracker positions in /signals."""
    lc = _lifecycle_dict(lifecycle)
    phase = str(lc.get("phase") or "")
    bias = str(lc.get("recommended_bias") or "")

    if phase == "no_setup":
        return "💡 Auto-invalidate через 3 тика — lifecycle no_setup"
    if direction == "short" and phase == "post_dump_bounce":
        if sig.get("tp1_hit"):
            return "💡 Auto-invalidate — bounce + TP1 (тезис шорта исчерпан)"
        return "💡 Auto-invalidate через 3 тика — post_dump_bounce против шорта"
    if direction == "short" and lc.get("invalidate_short"):
        return "💡 Рекомендация: invalidate — lifecycle invalidate_short"
    if direction == "short" and bias == "long":
        return "💡 Тезис устарел (bias long) — держи по latch, новый шорт не уйдёт"
    if direction == "long" and bias == "short":
        return "💡 Тезис устарел (bias short) — держи по latch, новый лонг не уйдёт"
    if direction == "long" and phase in {"distribution", "exhaustion_at_high"}:
        return "💡 Лонг против distribution — рассмотри фиксацию / invalidate"
    if sig.get("tp1_hit") and not sig.get("tp2_hit"):
        pct = sig.get("partial_fixed_pct") or 80
        if sig.get("sl_at_breakeven"):
            return (
                f"💡 TP1 взят — зафиксируй {pct}% · SL на entry (безубыток) · "
                f"остаток на TP2"
            )
        return f"💡 TP1 взят — зафиксируй {pct}% · остаток на TP2 / SL"
    if not bool(setup.get("confirmed")) and _setup_fuel(setup, direction) >= effective_hunt_params(
        symbol
    ).confirm_min_score:
        return "💡 Fuel достаточен — жди closed-bar confirm для re-alert"
    return None


def format_setup_snapshot(
    setup: dict[str, Any],
    *,
    direction: str,
    latch_score: Any,
    lifecycle: Any | None = None,
) -> str:
    """Compact live setup line — avoids duplicating the primary block reason."""
    lc = _lifecycle_dict(lifecycle)
    fuel = _setup_fuel(setup, direction)
    phase = str(setup.get("phase") or "—")
    latch = latch_score if latch_score not in (None, "", "—") else "—"
    confirm = "да" if bool(setup.get("confirmed")) else "нет"
    bias = str(lc.get("recommended_bias") or "—")
    return (
        f"Сетап: confirm={confirm} · fuel {fuel:.0f} (открыт {latch}) · "
        f"{phase} · bias={bias}"
    )


def evaluate_alert_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> GateResult:
    """Mirror watch._should_alert with explicit Russian explanation."""

    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    r = row or {}

    if not bool(setup.get("confirmed")):
        return GateResult(False, "not_confirmed", "Нет closed-bar confirm (5m/1m)")

    core = _core_lifecycle_blockers(setup, direction=direction, lc=lc)
    if core is not None:
        return core

    phase = str(lc.get("phase") or "")
    pm_blocked, pm_reason = phase_matrix_gate(phase, direction)
    if pm_blocked:
        return GateResult(False, "phase_matrix_disable", pm_reason)

    fuel = _setup_fuel(setup, direction)
    if fuel < cal.forming_min_score:
        return GateResult(
            False,
            "below_forming_min",
            f"Fuel {fuel:.0f} < forming_min {cal.forming_min_score:.0f}",
        )

    blocks = _hard_filter_blocks(
        setup.get("filter_blocks") or [],
        direction=direction,
        phase=str(lc.get("phase") or ""),
        fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
    )
    if blocks:
        txt = ", ".join(str(b) for b in blocks)
        return GateResult(False, "filter_block", f"Фильтр тренда/VWAP: {txt}")

    if not _passes_meme_anomaly_gate(sym=sym, row=r, lc=lc, cal=cal, fuel=fuel):
        chg24 = _row_chg24_abs(r)
        rng24 = _row_rng24(r)
        return GateResult(
            False,
            "not_anomaly",
            f"Не meme-аномалия: chg24={chg24:.1f}% range={rng24:.1f}% "
            f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
        )

    px = float(r.get("price") or 0)
    min_rr = _effective_min_rr(
        setup, direction=direction, symbol=sym, lc=lc, fuel=fuel, cal=cal
    )
    if _tp2_room_blocks(
        setup, price=px, min_room_pct=cal.tp2_min_room_pct, min_rr=min_rr
    ):
        return GateResult(
            False,
            "tp2_too_close",
            f"TP2 слишком близко ({cal.tp2_min_room_pct:.0f}% room min)",
        )

    if setup.get("levels_viable") is False:
        veto = setup.get("levels_veto") or []
        return GateResult(
            False,
            "levels_veto",
            f"Уровни не viable: {', '.join(str(v) for v in veto) or 'структура'}",
        )

    rr = setup.get("risk_reward")
    if rr is not None and float(rr) < min_rr:
        return GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")

    if direction == "short":
        phase = str(lc.get("phase") or "—")
        if phase == "post_dump_bounce":
            return GateResult(
                False,
                "short_blocked_bounce",
                "Шорт в post_dump_bounce запрещён — отскок после дампа, не fade",
            )
        if lc.get("invalidate_short"):
            return GateResult(
                False,
                "invalidate_short",
                "Lifecycle: отскок/пробой вверх — шорт инвалидирован",
            )
        late = _short_dump_delivery_too_late(lc, setup, symbol=sym)
        if late is not None:
            return late
        if not lc.get("short_entry_ok", False) and not _dump_continuation_short_ok(
            setup,
            phase=phase,
            lc=lc,
            fuel=fuel,
            cal_min_fuel=cal.confirm_min_score,
        ):
            bias = str(lc.get("recommended_bias") or "—")
            return GateResult(
                False,
                "short_entry_not_ok",
                f"Lifecycle {phase} bias={bias} — вход в шорт запрещён",
            )
        session = r.get("session") or {}
        tf = r.get("timeframes") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        blocked, prem_reason = blocks_premature_exhaustion_short(
            phase=str(lc.get("phase") or ""),
            fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
            bounce_from_low_pct=float(lc.get("bounce_from_low_pct") or 0),
            pos_in_range=float(session.get("pos_in_range") or 0.5),
            has_bear_div=has_bear_div,
            symbol=sym,
        )
        if blocked:
            hard = setup.get("confirm_hard") or []
            if not any("close_below_support" in str(h) for h in hard):
                return GateResult(
                    False,
                    "premature_exhaustion",
                    f"Ранний fade-at-top: {prem_reason}",
                )

    if direction == "long":
        phase = str(lc.get("phase") or "")
        fall = float(lc.get("fall_from_high_pct") or 0)
        if phase == "dump_active":
            return GateResult(
                False,
                "long_blocked_mid_dump",
                "Лонг в mid-dump запрещён — жди post_dump_bounce",
            )
        if phase not in {"post_dump_bounce", "impulse_initiating", "breakout_arming"}:
            hunt_high = float(
                r.get("impulse_high") or ((r.get("impulse") or {}).get("hunt_high")) or 0
            )
            price = float(r.get("price") or 0)
            if hunt_high > 0 and price > 0 and price < hunt_high * 0.90 and fall >= 12.0:
                return GateResult(
                    False,
                    "long_below_hunt_high",
                    f"Цена {price:.4g} < 90% hunt_high при fall {fall:.0f}%",
                )
        res = float(setup.get("resistance_break_level") or 0)
        px = float(r.get("price") or 0)
        r5_close = float((r.get("timeframes") or {}).get("5m_closed", {}).get("close") or 0)
        from hunt_core.scan._confirm_shared import long_resistance_chase_veto  # noqa: PLC0415

        if long_resistance_chase_veto(res, px, r5_close):
            return GateResult(
                False,
                "long_below_resistance",
                f"Цена {px:.4g} ниже resistance_break {res:.4g}",
            )

    delivery_block = _delivery_quality_gate(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lc,
        fuel=fuel,
        row=r,
    )
    if delivery_block is not None:
        return delivery_block

    return GateResult(True, "ok", "Все гейты пройдены — алерт разрешён")


def evaluate_formation(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
) -> GateResult:
    """Pre-confirm setup state for /signals and logs."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    fuel = _setup_fuel(setup, direction)
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))

    if confirmed:
        return GateResult(True, "confirmed", f"Confirm есть · phase={phase} · fuel={fuel:.0f}")

    if fuel < cal.forming_min_score:
        return GateResult(
            False,
            "forming_low",
            f"Формирование слабое: fuel {fuel:.0f} < {cal.forming_min_score:.0f}",
        )

    gaps: list[str] = []
    if not (setup.get("confirm_hard") or []):
        gaps.append("нет structural hard")
    if fuel < cal.confirm_min_score:
        gaps.append(f"fuel {fuel:.0f} < confirm {cal.confirm_min_score:.0f}")
    gap_txt = ", ".join(gaps) if gaps else "ждём closed-bar"
    bias = str(lc.get("recommended_bias") or "—")
    return GateResult(
        False,
        "forming",
        f"Формируется {phase} · fuel={fuel:.0f} · bias={bias} · {gap_txt}",
    )



GateChecker = Callable[..., GateResult | None]

_GATE_REGISTRY: list[tuple[str, GateChecker]] = []


def register_gate(name: str, fn: GateChecker) -> None:
    """Register a gate checker — first failure short-circuits the pipeline."""
    if any(existing == name for existing, _ in _GATE_REGISTRY):
        return
    _GATE_REGISTRY.append((name, fn))


def _gate_edge_policy(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    _ = setup, row, lifecycle, symbol, sniper_config
    edge_block = direction_block_reason(direction)
    if edge_block:
        return GateResult(ok=False, code=edge_block, message=edge_block)
    return None


def _gate_stale(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    _ = lifecycle, symbol, sniper_config
    stale = delivery_hard_block(direction=direction, setup=setup, row=row)
    if stale:
        return GateResult(
            ok=False,
            code=stale,
            message="Setup устарел — цена уже за TP1 или нет геометрии входа",
        )
    return None


def _gate_sniper(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: "SniperConfig | None" = None,
) -> GateResult | None:
    from hunt_core.deliver.dispatch import SniperConfig, sniper_block_reason

    _ = symbol
    sniper = sniper_block_reason(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lifecycle,
        config=sniper_config or SniperConfig.from_env(),
    )
    if sniper:
        return GateResult(ok=False, code=sniper, message=sniper)
    return None


def _gate_wash(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    _ = setup, sniper_config
    sym = symbol or str(row.get("symbol", ""))
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    wash = wash_block_reason(row=row, lifecycle=lc)
    if wash:
        record_funnel_stage("wash", symbol=sym, direction=direction, detail=wash)
        return GateResult(
            ok=False,
            code=wash,
            message="Подозрение на wash / манипуляцию объёмом",
        )
    return None


def _gate_kinematic(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    _ = setup, sniper_config
    sym = symbol or str(row.get("symbol", ""))
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    chase = kinematic_block_reason(row=row, direction=direction, lifecycle_phase=phase)
    if chase:
        record_funnel_stage("kinematic", symbol=sym, direction=direction, detail=chase)
        return GateResult(
            ok=False,
            code=chase,
            message="Слишком быстрое движение — поздний вход",
        )
    return None


def _register_builtin_gates() -> None:
    register_gate("edge_policy", _gate_edge_policy)
    register_gate("stale", _gate_stale)
    register_gate("sniper", _gate_sniper)
    register_gate("wash", _gate_wash)
    register_gate("kinematic", _gate_kinematic)


_register_builtin_gates()


def run_gate_pipeline(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
    fast_lane: bool = False,
) -> GateResult:
    """Run registered gates, then legacy alert gate."""
    sym = symbol or str(row.get("symbol", ""))
    skip = {"wash", "kinematic"} if fast_lane else set()
    for name, checker in _GATE_REGISTRY:
        if name in skip:
            continue
        blocked = checker(
            direction=direction,
            setup=setup,
            row=row,
            lifecycle=lifecycle,
            symbol=sym,
            sniper_config=sniper_config,
        )
        if blocked is not None and not blocked.ok:
            return blocked
    gate = evaluate_alert_gate(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lifecycle,
        row=row,
    )
    if gate.ok:
        record_funnel_stage(
            "deliver",
            symbol=sym,
            direction=direction,
            detail=gate.code or "ok",
            payload={
                "score": setup.get("dump_score") or setup.get("long_score"),
                "fuel": setup.get("dump_fuel") or setup.get("long_fuel"),
                "phase": setup.get("phase") or setup.get("lifecycle_phase"),
                "delivery_tier": setup.get("delivery_tier"),
                "risk_reward": setup.get("risk_reward"),
                "gate_code": gate.code or "ok",
            },
        )
    return gate

__all__ = [
    "DEFAULT_MAX_WR",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_PRIOR_WR",
    "PhaseStats",
    "disabled_phase_pairs",
    "phase_matrix_gate",
    "DeliveryTier",
    "classify_delivery_tier",
    "delivery_hard_block",
    "tp1_progress_block",
]
