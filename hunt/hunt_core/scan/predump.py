"""Pre-dump scanner path (§4.1 — CONFIRM short cascade)."""
from __future__ import annotations

from typing import Any


from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    dump_fast_confirm_enabled,
    effective_hunt_params,
    entry_confirm_tf,
    liquidation_thresholds,
    listings_thresholds,
)


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)


import hunt_core.scan._confirm_shared as _confirm_shared

globals().update(
    {k: v for k, v in vars(_confirm_shared).items() if not k.startswith("__")}
)
from hunt_core.scan.predump_dump_hunt import (
    DumpHuntTier,
    dump_hunt_cooldown_ok,
    dump_hunt_skip_reason,
    format_dump_hunt_telegram,
    mark_dump_hunt_sent,
    maybe_send_dump_hunt_telegram,
    score_dump_init,
    tier_from_verdict,
    display_short_setup,
)

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
    """Confirmed dump = structural hard + fuel floor + second factor (no score self-confirm)."""

    if dump.get("levels_viable") is False:
        return False, ["veto_levels:" + ",".join(dump.get("levels_veto") or [])]
    lst = listings_thresholds(symbol)
    bars_1h = int(dump.get("bars_1h") or 0)
    if dump.get("young_listing") and bars_1h < int(lst.get("min_1h_bars_confirm", 24)):
        return False, ["veto_young_listing_insufficient_bars"]
    lc = dump.get("lifecycle") if isinstance(dump.get("lifecycle"), dict) else {}
    lc_phase = str(dump.get("lifecycle_phase") or lc.get("phase") or "")
    fall_pct = float(lc.get("fall_from_high_pct") or dump.get("fall_from_high_pct") or 0)
    bounce_pct = float(lc.get("bounce_from_low_pct") or dump.get("bounce_from_low_pct") or 0)
    dump_continuation = lc_phase in {"dump_active", "distribution"} and fall_pct >= 15.0
    mkt = market or {}
    from hunt_core.gate.policy import mtf_confirm_veto  # noqa: PLC0415

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
    if dump.get("level_expired"):
        from hunt_core.gate.policy import check_mtf_structure_break  # noqa: PLC0415

        allowed, sb_reason = check_mtf_structure_break("short", tf, level_expired=True)
        if not allowed:
            return False, [f"veto_{sb_reason}"]
    if lifecycle_bias == "long" and not dump_continuation:
        return False, ["veto_lifecycle_bias_long"]
    if lifecycle_bias == "wait" and not dump_continuation:
        return False, ["veto_lifecycle_bias_wait"]
    phase_4h = _resolve_lifecycle_4h(dump)
    blocked_htf, htf_reason = _htf_bias_override(phase_4h, "short")
    if blocked_htf:
        return False, [f"veto_{htf_reason}"]
    hard: list[str] = []
    c5 = _closed_candle(tf, "5m")
    c1 = _closed_candle(tf, "1m")
    _closed_tf_close(tf, "5m")
    r15_rsi = _required_closed_rsi(tf, "15m")
    if r15_rsi is None:
        return False, ["veto_data_missing_rsi15m"]
    support = dump.get("support_break_level") or 0.0
    entry_tf = entry_confirm_tf(symbol, direction="short")
    hard.extend(
        _structural_close_break_triggers(
            direction="short",
            level=float(support or 0),
            tf=tf,
            entry_tf=entry_tf,
        )
    )
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.35 and r15_rsi >= 65:
        hard.append("5m_rejection_exhaustion")
    if c1.get("bearish") and c5.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bear_cascade")
    r15_closed = _closed_tf_block(tf, "15m")
    r1h_closed = _closed_tf_block(tf, "1h")
    if r15_closed.get("closed_bar") and r15_closed.get("pp_short_true"):
        hard.append("pp_short_break")
    elif r1h_closed.get("closed_bar") and r1h_closed.get("pp_short_true"):
        hard.append("pp_short_break")
    hard.extend(
        candle_pattern_hard_triggers(dump, direction="short", tf=tf, price=float(price or 0))
    )

    liq_score = mkt.get("liquidation_score_5m")
    if liq_score is None:
        liq_score = mkt.get("liquidation_score_1m")
    lt = liquidation_thresholds(symbol)
    liq_thr = float(lt.get("score_threshold", 0.30))
    min_ln = float(lt.get("min_long_notional_5m_usd", 25000.0))
    ln_notional = mkt.get("liquidation_long_notional_5m")
    try:
        ln_val = float(ln_notional) if ln_notional is not None else 0.0
    except (TypeError, ValueError):
        ln_val = 0.0
    if liq_score is not None and float(liq_score) <= liq_thr:
        if ln_val >= min_ln:
            hard.append("ws_liq_cascade_long_flush")
        else:
            hard.append("ws_liq_cascade_score_only")

    fuel = float(dump.get("dump_fuel") or 0)
    r1h = _closed_tf_block(tf, "1h") or {}
    r4h = _closed_tf_block(tf, "4h") or {}
    div = (
        r1h.get("bearish_rsi_div")
        or r4h.get("bearish_rsi_div")
        or r1h.get("bearish_macd_div")
        or r4h.get("bearish_macd_div")
    )
    triggers = dump.get("triggers") or []
    structural = [h for h in hard if _is_structural_confirm_trigger(h)]
    structural.extend(h for h in hard if "engulfing" in h)
    depth_imb = _resolve_depth_imbalance(mkt)
    ask_heavy = isinstance(depth_imb, (int, float)) and float(depth_imb) <= -0.10
    secondary = sum(
        1
        for cond in (
            bool(div),
            "oi_flush" in triggers,
            "dump_continuation" in triggers,
            any(str(t).startswith("ws_liq_cascade") for t in triggers),
            any(str(t).startswith("lost_support") for t in triggers),
            ask_heavy,
        )
        if cond
    )
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
        # Fast dump confirm: on a sub-5m confirm TF a single closed break + 1 secondary
        # is enough — a 5–8% dump completes in minutes, waiting 2× 5m bars misses it.
        if (
            not confirmed
            and fuel >= cal.confirm_min_score
            and closed_break
            and secondary >= 1
            and entry_tf in {"1m", "3m"}
            and dump_fast_confirm_enabled(symbol)
        ):
            confirmed = True
            hard.append("dump_fast_confirm")
        px = float(price or 0)
        if px > 0 and fuel >= cal.confirm_min_score and not confirmed:
            from hunt_core.gate.delivery import price_in_entry_zone  # noqa: PLC0415

            in_zone = price_in_entry_zone(dump, px, direction="short")
            ez = dump.get("entry_zone") or []
            try:
                zone_hi = float(ez[1])
            except (TypeError, ValueError, IndexError):
                zone_hi = 0.0
            near_zone_top = zone_hi > 0 and px >= zone_hi * 0.97
            if (in_zone or near_zone_top) and closed_break:
                confirmed = len(structural) >= 1 and (
                    secondary >= 1 or len(structural) >= 2
                )
            elif (in_zone or near_zone_top) and any(
                "cascade" in h for h in structural
            ):
                confirmed = len(structural) >= 1
    # Peak fade (manual trader: waiting for the structure break is already late). At
    # exhaustion_at_high a rejection wick + exhaustion confluence (divergence or a
    # secondary factor) confirms the fade WITHOUT a structure break. The delivery gate
    # still enforces fuel>=78 / div / ADX<=32 and the premature-fade guard, so this only
    # unblocks legitimate top fades — not blind knife-catches on a vertical pump.
    if not confirmed and fuel >= cal.confirm_min_score and lc_phase in {
        "exhaustion_at_high",
        "distribution",
    }:
        if fall_pct <= 3.0 and bool(div):
            confirmed = True
            hard.append("pre_dump_div_confirm")
        elif lc_phase == "exhaustion_at_high":
            rejection = any("rejection" in h for h in hard)
            if rejection and (bool(div) or secondary >= 1):
                confirmed = True
                hard.append("peak_fade_confirm")
    aligned, of_reason = _orderflow_confirm_aligned("short", mkt, symbol=symbol)
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
    if has_initiation and fuel >= cal.confirm_min_score:
        return "dump_imminent"
    if has_initiation and fuel >= cal.forming_min_score:
        return "dump_initiating"
    if fuel >= cal.forming_min_score:
        return "dump_setup_forming"
    if fuel >= 25:
        return "exhaustion_watch"
    return "no_dump_yet"



def enrich_dump_setup(
    dump: dict[str, Any],
    *,
    price: float = 0.0,
    tf: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sym = str(dump.get("symbol") or "")
    _apply_ema200_confluence(
        dump, direction="short", score_key="dump_score", price=price, tf=tf, symbol=sym
    )
    _apply_squeeze_at_boundary(
        dump, direction="short", score_key="dump_score", tf=tf, symbol=sym
    )
    _apply_hidden_div_fuel(dump, direction="short", score_key="dump_score", tf=tf)
    _apply_chart_pattern_fuel(dump, direction="short", score_key="dump_score", tf=tf)
    _apply_polars_ta_fuel(dump, direction="short", score_key="dump_score", tf=tf)
    _apply_distribution_fuel(dump, direction="short", score_key="dump_score", tf=tf)
    _apply_research_fuel(dump, direction="short", score_key="dump_score", tf=tf)
    _apply_candle_pattern_fuel(
        dump, direction="short", score_key="dump_score", tf=tf, price=price
    )
    _apply_ws_orderflow_fuel(dump, direction="short", score_key="dump_score", market=market)
    level = float(dump.get("support_break_level") or dump.get("local_support") or 0)
    _apply_prokol_fuel_penalty(
        dump, direction="short", tf=tf, level=level
    )
    dump["dump_fuel"] = compute_setup_fuel(dump, direction="short", symbol=sym, tf=tf)
    return dump




def evaluate_predump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    dump = dict(row.get("dump") or {})
    dump = enrich_dump_setup(dump, price=price, tf=tf, market=market)
    sym = str(row.get("symbol") or "")
    cal = effective_hunt_params(sym)
    confirmed, _hard = confirm_dump(dump, tf=tf, market=market, symbol=sym, price=price, cal=cal)
    dump["confirmed"] = confirmed
    dump["phase"] = phase_dump(dump, confirmed, cal=cal)
    return dump


__all__ = [
    "DumpHuntTier",
    "confirm_dump",
    "display_short_setup",
    "dump_hunt_cooldown_ok",
    "dump_hunt_skip_reason",
    "enrich_dump_setup",
    "evaluate_predump",
    "format_dump_hunt_telegram",
    "mark_dump_hunt_sent",
    "maybe_send_dump_hunt_telegram",
    "phase_dump",
    "score_dump_init",
    "tier_from_verdict",
]
