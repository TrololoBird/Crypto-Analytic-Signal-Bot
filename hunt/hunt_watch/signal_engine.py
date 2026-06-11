"""Hunt signal scoring — fuel vs initiation vs confirmation (decoupled).

dump_score/long_score remain raw additive telemetry.
dump_fuel / long_fuel = cluster-capped readiness (0–100).
Phases and confirmed flags use fuel + structural hard triggers only.
"""

from __future__ import annotations

from typing import Any

from hunt_watch.market_regime import HuntCalibratedParams
from hunt_watch.mtf_policy import closed_rsi, mtf_confirm_veto
from hunt_watch.param_store import (
    confirm_thresholds,
    liquidation_thresholds,
    listings_thresholds,
    orderflow_thresholds,
)

# Cluster caps prevent correlated triggers (RSI15+RSI1H+div+funding) inflating fuel.
_CLUSTER_CAP = 28.0
_FUEL_CLUSTER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "exhaustion",
        (
            "rsi15_overbought",
            "rsi1h_overbought",
            "rsi15_oversold",
            "rsi1h_oversold",
            "bear_div",
            "bull_div",
            "rejection",
            "bounce",
            "overbought",
            "oversold",
            "wick",
            "at_fib",
            "extended",
            "crowded_long_funding",
            "crowded_short_funding",
        ),
    ),
    (
        "structure",
        (
            "lost_support",
            "below_impulse",
            "broke_resistance",
            "deep_below",
            "distribution",
            "close_below",
            "close_above",
        ),
    ),
    (
        "flow",
        (
            "taker_",
            "oi_flush",
            "oi_build",
            "microprice",
            "global_ls",
            "crowded_longs",
            "crowded_shorts",
            "oi_build_z",
            "oi_flush_z",
        ),
    ),
    (
        "micro",
        (
            "ws_liq",
            "ws_taker",
            "spot_lead",
            "regime_",
        ),
    ),
)

_INITIATION_HARD_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
        "5m_rejection_exhaustion",
        "ws_liq_cascade_long_flush",
    }
)

_INITIATION_HARD_LONG = frozenset(
    {
        "5m_close_above_resistance",
        "15m_close_above_resistance",
        "1m_5m_bull_cascade",
        "5m_bounce_oversold",
    }
)

_STRUCTURAL_CONFIRM_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
    }
)


def long_resistance_chase_veto(
    resistance: float,
    price: float,
    r5_close: float,
) -> bool:
    """Veto late long chase; allow 0.5% retest when 5m closed above resistance."""
    if resistance <= 0 or price <= 0:
        return False
    ratio = 0.995 if r5_close > resistance else 0.998
    return price < resistance * ratio


def _cluster_for_trigger(trigger: str) -> str | None:
    t = str(trigger).lower()
    for cluster, needles in _FUEL_CLUSTER_RULES:
        if any(n in t for n in needles):
            return cluster
    return None


def cluster_fuel(triggers: list[str], *, raw_score: float) -> float:
    """Deduplicated fuel: sum of per-cluster contributions, each capped."""
    buckets: dict[str, float] = {c: 0.0 for c, _ in _FUEL_CLUSTER_RULES}
    for trig in triggers:
        cluster = _cluster_for_trigger(trig)
        if cluster is None:
            continue
        # Weight by trigger severity keywords
        w = 12.0
        if "lost_support" in trig or "broke_resistance" in trig:
            w = 28.0
        elif "close_below" in trig or "close_above" in trig or "cascade" in trig:
            w = 22.0
        elif "div" in trig:
            w = 18.0
        elif "rejection" in trig or "bounce" in trig:
            w = 16.0
        buckets[cluster] = min(_CLUSTER_CAP, buckets[cluster] + w)
    fuel = sum(buckets.values())
    # Never exceed raw by much; never above 100
    return round(min(100.0, max(fuel, min(raw_score * 0.55, 100.0))), 1)


def _orderflow_confirm_aligned(
    direction: str,
    mkt: dict[str, Any],
    *,
    symbol: str = "",
) -> tuple[bool, str]:
    """60s taker delta must align with confirm direction when WS data is present."""
    of = orderflow_thresholds(symbol)
    if not of.get("require_ws_align", True):
        return True, ""
    agg60 = mkt.get("agg_trade_delta_60s")
    if agg60 is None:
        return True, ""
    try:
        val = float(agg60)
    except (TypeError, ValueError):
        return True, ""
    buy_min = float(of.get("taker_buy_min", 0.58))
    sell_max = float(of.get("taker_sell_max", 0.42))
    if direction == "long" and val < buy_min:
        return False, "orderflow_sell_pressure_vs_long"
    if direction == "short" and val > sell_max:
        return False, "orderflow_buy_pressure_vs_short"
    return True, ""


def confirm_dump(
    dump: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    cal: HuntCalibratedParams,
    lifecycle_bias: str = "",
) -> tuple[bool, list[str]]:
    """Confirmed dump = structural hard + fuel floor + second factor (no score self-confirm).

    Hard vetoes: non-viable levels (risk math broke) and lifecycle bias "wait" —
    a signal the lifecycle itself says not to trade must never reach Telegram
    (HOMEUSDT/JCTUSDT/BTWUSDT were delivered with bias=wait).
    """
    if dump.get("levels_viable") is False:
        return False, ["veto_levels:" + ",".join(dump.get("levels_veto") or [])]
    lst = listings_thresholds(symbol)
    bars_1h = int(dump.get("bars_1h") or 0)
    if dump.get("young_listing") and bars_1h < int(lst.get("min_1h_bars_confirm", 24)):
        return False, ["veto_young_listing_insufficient_bars"]
    # Counter-bias veto: lifecycle says bounce phases (bias=long) — a fresh
    # short confirm there is fighting the structure. The mid-dump "wait" case
    # is enforced at DELIVERY via short_entry_ok, not here: dump_confirmed is
    # still valid monitoring state mid-dump.
    lc = dump.get("lifecycle") if isinstance(dump.get("lifecycle"), dict) else {}
    lc_phase = str(dump.get("lifecycle_phase") or lc.get("phase") or "")
    fall_pct = float(lc.get("fall_from_high_pct") or dump.get("fall_from_high_pct") or 0)
    bounce_pct = float(lc.get("bounce_from_low_pct") or dump.get("bounce_from_low_pct") or 0)
    # Structural dump continuation: ≥15% off hunt_high — bias wait/long must not veto
    # (BEAT 8.37→6.7: phase=post_dump_bounce, bias=long, zero TG alerts).
    dump_continuation = lc_phase in {"dump_active", "distribution"} and fall_pct >= 15.0
    mkt = market or {}
    blocked, mtf_reason = mtf_confirm_veto(
        "short",
        tf,
        lc_phase,
        market=mkt,
        fall_from_high_pct=fall_pct,
        bounce_from_low_pct=bounce_pct,
    )
    if blocked:
        return False, [f"veto_{mtf_reason}"]
    if lifecycle_bias == "long" and not dump_continuation:
        return False, ["veto_lifecycle_bias_long"]
    if lifecycle_bias == "wait" and not dump_continuation:
        return False, ["veto_lifecycle_bias_wait"]
    hard: list[str] = []
    c5 = tf.get("5m_closed", {}).get("candle", {})
    c1 = tf.get("1m_closed", {}).get("candle", {})
    r5_close = tf.get("5m_closed", {}).get("close") or 0.0
    r15_rsi = closed_rsi(tf, "15m", default=0.0)
    support = dump.get("support_break_level") or 0.0
    r15_close = tf.get("15m_closed", {}).get("close") or 0.0

    if support and r5_close < support:
        hard.append("5m_close_below_support")
    if support and r15_close and r15_close < support:
        hard.append("15m_close_below_support")
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.35 and r15_rsi >= 65:
        hard.append("5m_rejection_exhaustion")
    if c1.get("bearish") and c5.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bear_cascade")

    liq_score = mkt.get("liquidation_score_1m") or mkt.get("liquidation_score_5m")
    lt = liquidation_thresholds(symbol)
    liq_thr = float(lt.get("score_threshold", 0.30))
    min_ln = float(lt.get("min_long_notional_5m_usd", 25000.0))
    ln_notional = mkt.get("liquidation_long_notional_5m")
    try:
        ln_val = float(ln_notional) if ln_notional is not None else 0.0
    except (TypeError, ValueError):
        ln_val = 0.0
    if liq_score is not None and float(liq_score) <= -liq_thr:
        if ln_val >= min_ln:
            hard.append("ws_liq_cascade_long_flush")
        else:
            hard.append("ws_liq_cascade_score_only")

    # Fuel only — the raw additive score self-inflates on correlated triggers.
    # If fuel is missing the setup did not pass enrichment: not confirmable.
    fuel = float(dump.get("dump_fuel") or 0)
    div = tf.get("1h", {}).get("bearish_rsi_div") or tf.get("4h", {}).get("bearish_rsi_div")
    triggers = dump.get("triggers") or []
    structural = [h for h in hard if h in _STRUCTURAL_CONFIRM_DUMP]
    secondary = sum(
        1
        for cond in (
            bool(div),
            "oi_flush" in triggers,
            "dump_continuation" in triggers,
            any(str(t).startswith("ws_liq_cascade") for t in triggers),
            any(str(t).startswith("lost_support") for t in triggers),
        )
        if cond
    )
    # 2 independent structural triggers, or 1 structural CLOSED-bar break
    # backed by >=2 secondary factors — one wick + one factor is not a dump.
    closed_break = any("close_below_support" in h for h in structural)
    ct = confirm_thresholds(symbol)
    bounce_min = float(ct.get("short_bounce_recovery_bounce_min_pct", 8.0))
    fall_max = float(ct.get("short_bounce_recovery_fall_max_pct", 15.0))
    bounce_recovery = (
        lc_phase in {"accumulation", "recovery"}
        and bounce_pct >= bounce_min
        and fall_pct < fall_max
    )
    if bounce_recovery:
        confirmed = fuel >= cal.confirm_min_score and len(structural) >= 2
    else:
        confirmed = fuel >= cal.confirm_min_score and (
            len(structural) >= 2 or (closed_break and secondary >= 2)
        )
    aligned, of_reason = _orderflow_confirm_aligned("short", mkt, symbol=symbol)
    if not aligned:
        return False, [f"veto_{of_reason}"]
    return confirmed, hard


_LONG_PUMP_PHASES = frozenset(
    {
        "breakout_arming",
        "impulse_initiating",
        "post_dump_bounce",
        "accumulation",
        "recovery",
    }
)


def confirm_long(
    long_setup: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    cal: HuntCalibratedParams,
    lifecycle_bias: str = "",
    lifecycle_phase: str = "",
) -> tuple[bool, list[str]]:
    if long_setup.get("levels_viable") is False:
        return False, ["veto_levels:" + ",".join(long_setup.get("levels_veto") or [])]
    lst = listings_thresholds(symbol)
    bars_1h = int(long_setup.get("bars_1h") or 0)
    if long_setup.get("young_listing") and bars_1h < int(lst.get("min_1h_bars_confirm", 24)):
        return False, ["veto_young_listing_insufficient_bars"]
    lc_phase = str(
        lifecycle_phase or long_setup.get("lifecycle_phase") or ""
    )
    # Counter-bias veto: exhaustion/distribution (bias=short) — no fresh long
    # confirm there. Initial pump phases (BEAT Jun6 / VELVET base breakout) pass.
    if lifecycle_bias in {"short", "wait"} and lc_phase not in _LONG_PUMP_PHASES:
        veto = "veto_lifecycle_bias_short" if lifecycle_bias == "short" else "veto_lifecycle_bias_wait"
        return False, [veto]
    hard: list[str] = []
    resistance = long_setup.get("resistance_break_level") or 0.0
    c1 = tf.get("1m_closed", {}).get("candle", {})
    c5 = tf.get("5m_closed", {}).get("candle", {})
    r5_close = tf.get("5m_closed", {}).get("close") or 0.0
    r15_close = tf.get("15m_closed", {}).get("close") or 0.0
    r15_rsi = closed_rsi(tf, "15m", default=50.0)
    blocked, mtf_reason = mtf_confirm_veto(
        "long",
        tf,
        lc_phase,
        fall_from_high_pct=float(long_setup.get("fall_from_high_pct") or 0),
    )
    if blocked:
        return False, [f"veto_{mtf_reason}"]

    if resistance and r5_close > resistance:
        hard.append("5m_close_above_resistance")
    # Symmetry with confirm_dump (15m_close_below_support): a 15m closed bar above
    # resistance is an independent structural confirmation, not a duplicate of 5m.
    if resistance and r15_close and r15_close > resistance:
        hard.append("15m_close_above_resistance")
    if long_resistance_chase_veto(resistance, price, r5_close):
        return False, ["veto_price_below_resistance"]
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35 and r15_rsi <= 40:
        hard.append("5m_bounce_oversold")
    if c1.get("bullish") and c5.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bull_cascade")

    fuel = float(long_setup.get("long_fuel") or 0)
    div = tf.get("1h", {}).get("bullish_rsi_div") or tf.get("4h", {}).get("bullish_rsi_div")
    triggers = long_setup.get("triggers") or []
    structural = [h for h in hard if h in _INITIATION_HARD_LONG]
    secondary = sum(
        1
        for cond in (
            bool(div),
            "oi_build" in triggers,
            any(str(t).startswith("broke_resistance") for t in triggers),
            any("ws_taker_buy" in str(t) for t in triggers),
            any("spot_lead_pump" in str(t) for t in triggers),
            lc_phase in {"impulse_initiating", "breakout_arming"},
        )
        if cond
    )
    closed_break = any("close_above_resistance" in h for h in structural)
    chg24 = float(long_setup.get("context_chg_24h_pct") or 0)
    pos_rng = float(long_setup.get("context_pos_in_range") or 0.5)
    weak_acc = (
        lc_phase == "accumulation"
        and chg24 < -8.0
        and pos_rng < 0.45
    )
    ct = confirm_thresholds(symbol)
    secondary_min = int(ct.get("accumulation_secondary_min", 3)) if weak_acc else 2
    if lc_phase == "accumulation" and closed_break and len(structural) < 2:
        confirmed = fuel >= cal.confirm_min_score and secondary >= secondary_min
    else:
        confirmed = fuel >= cal.confirm_min_score and (
            len(structural) >= 2 or (closed_break and secondary >= secondary_min)
        )
    mkt = market or {}
    aligned, of_reason = _orderflow_confirm_aligned("long", mkt, symbol=symbol)
    if not aligned:
        return False, [f"veto_{of_reason}"]
    return confirmed, hard


def phase_dump(
    dump: dict[str, Any],
    confirmed: bool,
    *,
    lifecycle_note: str | None = None,
    cal: HuntCalibratedParams,
) -> str:
    if lifecycle_note:
        return lifecycle_note
    if confirmed:
        return "dump_confirmed"
    fuel = float(dump.get("dump_fuel") or 0)
    hard = dump.get("confirm_hard") or []
    has_initiation = any(h in _INITIATION_HARD_DUMP for h in hard)
    if has_initiation and fuel >= cal.forming_min_score:
        return "dump_initiating"
    if fuel >= cal.confirm_min_score and has_initiation:
        return "dump_imminent"
    if fuel >= cal.forming_min_score:
        return "dump_setup_forming"
    if fuel >= 25:
        return "exhaustion_watch"
    return "no_dump_yet"


def phase_long(long_setup: dict[str, Any], confirmed: bool, *, cal: HuntCalibratedParams) -> str:
    if confirmed:
        return "long_confirmed"
    fuel = float(long_setup.get("long_fuel") or 0)
    hard = long_setup.get("confirm_hard") or []
    has_initiation = any(h in _INITIATION_HARD_LONG for h in hard)
    if has_initiation and fuel >= cal.forming_min_score:
        return "long_initiating"
    if fuel >= cal.confirm_min_score and has_initiation:
        return "long_imminent"
    if fuel >= cal.forming_min_score:
        return "long_setup_forming"
    if fuel >= 25:
        return "accumulation_watch"
    return "no_long_yet"


def enrich_dump_setup(dump: dict[str, Any]) -> dict[str, Any]:
    triggers = list(dump.get("triggers") or [])
    raw = float(dump.get("dump_score") or 0)
    dump["dump_fuel"] = cluster_fuel(triggers, raw_score=raw)
    return dump


def enrich_long_setup(setup: dict[str, Any]) -> dict[str, Any]:
    triggers = list(setup.get("triggers") or [])
    raw = float(setup.get("long_score") or 0)
    setup["long_fuel"] = cluster_fuel(triggers, raw_score=raw)
    chg24 = setup.get("context_chg_24h_pct")
    pos = setup.get("context_pos_in_range")
    phase = str(setup.get("lifecycle_phase") or "")
    if (
        phase == "accumulation"
        and chg24 is not None
        and float(chg24) < -8.0
        and pos is not None
        and float(pos) < 0.45
    ):
        setup["long_fuel"] = round(min(float(setup["long_fuel"]), 72.0), 1)
    return setup
