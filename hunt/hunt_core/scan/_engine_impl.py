"""Detection engine — scoring, routing, dump alerts, PP detect."""

from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    dump_fast_confirm_enabled,
    effective_hunt_params,
    entry_confirm_tf,
    liquidation_thresholds,
    listings_thresholds,
    orderflow_thresholds,
    scoring_thresholds,
)
from hunt_core.paths import ADAPTIVE_THRESHOLDS, DUMP_HUNT_ALERT_STATE, EWMA_THRESHOLDS, IGNITION_STATE
from hunt_core.errors import optional_finite_float, require_mark_price
from hunt_core.features.prepare import _swing_points


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)



DetectorPath = Literal["short_dump", "long_bounce", "early_advisory", "none"]


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    path: DetectorPath
    direction: str
    setup: dict[str, Any]
    row: dict[str, Any]
    lifecycle: dict[str, Any]


def route_tick(row: dict[str, Any]) -> list[SetupCandidate]:
    """Return candidate setups for this tick (H-B: all active paths)."""
    lifecycle = row.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    out: list[SetupCandidate] = []
    dump = row.get("dump") or {}
    long_b = row.get("long") or {}
    if isinstance(dump, dict) and (dump.get("confirmed") or dump.get("score")):
        out.append(
            SetupCandidate(
                path="short_dump",
                direction="short",
                setup=dump,
                row=row,
                lifecycle=lifecycle,
            )
        )
    if isinstance(long_b, dict) and (long_b.get("confirmed") or long_b.get("score")):
        out.append(
            SetupCandidate(
                path="long_bounce",
                direction="long",
                setup=long_b,
                row=row,
                lifecycle=lifecycle,
            )
        )
    phase = str(lifecycle.get("phase") or "")
    if phase in {"impulse_initiating", "post_dump_bounce", "distribution"}:
        out.append(
            SetupCandidate(
                path="early_advisory",
                direction=str(lifecycle.get("recommended_bias") or "short"),
                setup={"phase": phase, "advisory": True},
                row=row,
                lifecycle=lifecycle,
            )
        )
    return out


DeliveryMode = Literal["monitor_only", "armed_first", "confirm_first"]

_FORMING_PHASES = frozenset(
    {
        "dump_setup_forming",
        "long_setup_forming",
        "dump_initiating",
        "long_initiating",
        "dump_imminent",
        "long_imminent",
        "exhaustion_watch",
        "accumulation_watch",
    }
)
_ARMED_PHASES = frozenset(
    {
        "dump_active",
        "distribution",
        "post_dump_bounce",
        "accumulation",
        "long_active",
        "squeeze",
        "impulse_active",
    }
)


def resolve_delivery_mode(
    lifecycle: dict[str, Any],
    setup: dict[str, Any],
) -> DeliveryMode:
    """How delivery tier routing treats forming vs confirmed setups."""
    if setup.get("confirmed"):
        return "confirm_first"
    phase = str(lifecycle.get("phase") or setup.get("lifecycle_phase") or "")
    if phase in _ARMED_PHASES:
        return "armed_first"
    fuel = float(
        setup.get("dump_fuel")
        or setup.get("long_fuel")
        or setup.get("dump_score")
        or setup.get("long_score")
        or 0
    )
    if phase in _FORMING_PHASES and fuel >= 45:
        return "armed_first"
    if phase in {"", "no_dump_yet", "no_long_yet", "impulse_initiating"}:
        return "monitor_only"
    return "monitor_only" if fuel < 45 else "armed_first"



_SWING_N = 3
_TRUE_BODIES_MIN = 2
_EARLY_BODIES = 1
_MAX_PIVOT_AGE = 96


def _wick_zone(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    idx: int,
    *,
    side: Literal["high", "low"],
) -> tuple[float, float]:
    o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
    body_top = max(o, c)
    body_bot = min(o, c)
    if side == "high":
        return body_top, h
    return l, body_bot


def _bodies_beyond(
    opens: list[float],
    closes: list[float],
    *,
    start_idx: int,
    direction: Literal["below", "above"],
    level: float,
) -> int:
    count = 0
    for i in range(len(closes) - 1, start_idx, -1):
        body_top = max(opens[i], closes[i])
        body_bot = min(opens[i], closes[i])
        if direction == "below":
            if body_top < level:
                count += 1
            else:
                break
        elif body_bot > level:
            count += 1
        else:
            break
    return count


def _pp_side(
    work: pl.DataFrame,
    mask: pl.Series,
    *,
    side: Literal["high", "low"],
    closed: bool,
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work.is_empty():
        return empty

    end = work.height - (2 if closed and work.height >= 2 else 1)
    if end < _SWING_N + 2:
        return empty

    opens = [float(x) for x in work["open"].to_list()]
    highs = [float(x) for x in work["high"].to_list()]
    lows = [float(x) for x in work["low"].to_list()]
    closes = [float(x) for x in work["close"].to_list()]
    swing_mask = mask.to_list()

    pivot_idx: int | None = None
    for i in range(end - 1, max(_SWING_N, end - _MAX_PIVOT_AGE) - 1, -1):
        if i < len(swing_mask) and swing_mask[i]:
            pivot_idx = i
            break
    if pivot_idx is None:
        return empty

    zone_lo, zone_hi = _wick_zone(opens, highs, lows, closes, pivot_idx, side=side)
    if side == "high":
        bodies = _bodies_beyond(
            opens,
            closes,
            start_idx=pivot_idx,
            direction="below",
            level=zone_lo,
        )
        return {
            "pp_short_true": bodies >= _TRUE_BODIES_MIN,
            "pp_short_early": bodies == _EARLY_BODIES,
            "pp_long_true": False,
            "pp_long_early": False,
            "pp_short_zone_lo": round(zone_lo, 6),
            "pp_short_zone_hi": round(zone_hi, 6),
            "pp_short_bodies": bodies,
            "pp_short_swing_idx": pivot_idx,
        }

    bodies = _bodies_beyond(
        opens,
        closes,
        start_idx=pivot_idx,
        direction="above",
        level=zone_hi,
    )
    return {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": bodies >= _TRUE_BODIES_MIN,
        "pp_long_early": bodies == _EARLY_BODIES,
        "pp_long_zone_lo": round(zone_lo, 6),
        "pp_long_zone_hi": round(zone_hi, 6),
        "pp_long_bodies": bodies,
        "pp_long_swing_idx": pivot_idx,
    }


def detect_pp(work: pl.DataFrame, *, closed: bool = False) -> dict[str, Any]:
    """Detect PP short/long breaks on a single TF frame (1h or 15m)."""
    base: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work is None or work.is_empty():
        return base
    if not {"open", "high", "low", "close"}.issubset(set(work.columns)):
        return base

    sh_mask, sl_mask = _swing_points(work, n=_SWING_N, include_unconfirmed_tail=False)
    short_pp = _pp_side(work, sh_mask, side="high", closed=closed)
    long_pp = _pp_side(work, sl_mask, side="low", closed=closed)
    out = {**base, **short_pp, **long_pp}
    out["pp_short_true"] = short_pp.get("pp_short_true", False)
    out["pp_short_early"] = short_pp.get("pp_short_early", False)
    out["pp_long_true"] = long_pp.get("pp_long_true", False)
    out["pp_long_early"] = long_pp.get("pp_long_early", False)
    if not closed:
        out["pp_short_true"] = False
        out["pp_long_true"] = False
    return out

_TRIGGER_REASONS = frozenset({
    "1m_macd_cross_down",
    "1m_macd_hist_neg",
    "1m_macd_exhaust",
    "below_support",
    "hunt_short_confirmed",
})

_SETUP_REASON_PREFIXES = (
    "1h_rsi=",
    "15m_rsi=",
    "top_ls=",
    "funding_crowded=",
    "phase=",
)


def _fval(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _market_val(market: dict[str, Any], micro: dict[str, Any], key: str) -> Any:
    val = market.get(key)
    if val is not None:
        return val
    return micro.get(key)


def _setup_hits(reasons: list[str]) -> int:
    return sum(1 for r in reasons if r.startswith(_SETUP_REASON_PREFIXES))


def _has_trigger(reasons: list[str]) -> bool:
    return any(
        r in _TRIGGER_REASONS
        or r.startswith("fall_trigger=")
        or r.startswith("below_support=")
        for r in reasons
    )


def score_dump_init(
    *,
    row: dict[str, Any],
    micro: dict[str, Any],
    tf: dict[str, Any],
    prev: dict[str, Any] | None,
) -> tuple[int, list[str], str]:
    """Score 0-100 dump initiation readiness + human verdict."""
    reasons: list[str] = []
    score = 0
    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    market = row.get("market") or row.get("positioning") or {}
    price = float(row.get("price") or 0)

    phase = str(lc.get("phase") or "")
    fall = float(lc.get("fall_from_high_pct") or 0)
    hunt_high = float((row.get("impulse") or {}).get("hunt_high") or row.get("impulse_high") or 0)
    price_f = float(price or row.get("price") or 0)

    from hunt_core.regime.leg_fsm import pre_dump_zone  # noqa: PLC0415

    at_pre_dump = pre_dump_zone(
        price=price_f,
        hunt_high=hunt_high,
        fall_from_high_pct=fall,
    )
    if phase in ("exhaustion_at_high", "distribution"):
        score += 18
        reasons.append(f"phase={phase}")
    if at_pre_dump:
        score += 14
        reasons.append(f"pre_dump_fall={fall:.1f}%")
    elif fall >= 1.5:
        score += min(15, int(fall * 3))
        reasons.append(f"fall_from_high={fall:.1f}%")

    support = float(dump.get("support_break_level") or dump.get("invalidation_above") or 0)
    if support > 0 and price < support and fall >= 2.5:
        score += 20
        reasons.append(f"below_support={support:.5f}")

    # --- REST flow ---
    taker5 = _fval(_market_val(market, micro, "taker_5m"))
    if taker5 is not None and taker5 < 0.98:
        score += 12
        reasons.append(f"taker_5m_sell={taker5:.3f}")
    elif taker5 is not None and taker5 > 1.05:
        score -= 8
        reasons.append(f"taker_5m_buy={taker5:.3f}")

    oi1h = _fval(_market_val(market, micro, "oi_chg_1h"))
    if oi1h is not None and oi1h < -0.005:
        score += 10
        reasons.append(f"oi_flush_1h={oi1h:.3f}")

    oi_z = _fval(market.get("oi_z") or micro.get("oi_z"))
    if oi_z is not None and oi_z > 1.5:
        score += 5
        reasons.append(f"oi_z_elevated={oi_z}")

    fund = _fval(_market_val(market, micro, "funding_pct"))
    if fund is not None and fund >= 0.55:
        score += 10
        reasons.append(f"funding_crowded={fund:.3f}%")
    elif fund is not None and fund > 0.15:
        score += 8
        reasons.append(f"funding_crowded={fund:.3f}%")

    top_ls = _fval(_market_val(market, micro, "top_ls_1h"))
    if top_ls is not None and top_ls >= 1.85:
        score += 6
        reasons.append(f"top_ls={top_ls:.2f}")

    depth = _fval(market.get("depth_imbalance"))
    if depth is not None and depth <= -0.08:
        score += 8
        reasons.append(f"ask_heavy={depth:.2f}")

    # --- Polars MTF setup ---
    m1 = tf.get("1m") or {}
    m5 = tf.get("5m") or {}
    m15 = tf.get("15m") or {}
    m1h = tf.get("1h") or {}

    rsi1h = _fval(m1h.get("closed_rsi14") or m1h.get("rsi14"))
    if rsi1h is not None and rsi1h >= 88:
        score += 10
        reasons.append(f"1h_rsi={rsi1h:.0f}")

    rsi15 = _fval(m15.get("closed_rsi14") or m15.get("rsi14"))
    if rsi15 is not None and rsi15 >= 78:
        score += 8
        reasons.append(f"15m_rsi={rsi15:.0f}")

    rsi5 = _fval(m5.get("closed_rsi14") or m5.get("rsi14"))
    if rsi5 is not None and rsi5 > 70:
        score += 6
        reasons.append(f"5m_rsi={rsi5:.0f}")

    macd5 = _fval(m5.get("closed_macd_hist"))
    if macd5 is not None and macd5 < 0:
        score += 10
        reasons.append("5m_macd_hist_neg")

    # --- Phase 2: 1m MACD trigger (fast dump) ---
    macd1 = _fval(m1.get("closed_macd_hist") if m1.get("closed_macd_hist") is not None else m1.get("macd_hist"))
    if macd1 is not None:
        if macd1 < 0:
            score += 14
            reasons.append("1m_macd_hist_neg")
        elif macd1 <= 0.0002:
            score += 12
            reasons.append("1m_macd_exhaust")

    if prev and macd1 is not None:
        prev_m1 = (prev.get("timeframes") or {}).get("1m") or {}
        prev_macd = _fval(
            prev_m1.get("closed_macd_hist")
            if prev_m1.get("closed_macd_hist") is not None
            else prev_m1.get("macd_hist")
        )
        if prev_macd is not None and prev_macd > 0.0003 and macd1 <= 0:
            score += 16
            reasons.append("1m_macd_cross_down")

    if fall >= 4.0:
        score += 10
        reasons.append(f"fall_trigger={fall:.1f}%")

    if dump.get("confirmed"):
        score += 25
        reasons.append("hunt_short_confirmed")
    elif float(dump.get("dump_score") or 0) >= 68:
        score += 8
        reasons.append(f"dump_score={dump.get('dump_score')}")

    if prev:
        prev_fall = float((prev.get("lifecycle") or {}).get("fall_from_high_pct") or 0)
        if fall > prev_fall + 0.4:
            score += 8
            reasons.append(f"fall_accel +{fall - prev_fall:.1f}%")
        prev_price = float(prev.get("price") or 0)
        if prev_price > 0 and price < prev_price * 0.997:
            score += 6
            reasons.append("price_break_down")

    setup_hits = _setup_hits(reasons)
    trigger = _has_trigger(reasons) or bool(dump.get("confirmed"))

    score = max(0, min(100, score))
    if dump.get("confirmed"):
        verdict = "DUMP_LIKELY"
    elif score >= 85 and trigger and setup_hits >= 2:
        verdict = "DUMP_LIKELY"
    elif score >= 70 and (trigger or (setup_hits >= 3 and fall >= 2.0)):
        verdict = "DUMP_ARMED"
    elif score >= 50 and setup_hits >= 2:
        verdict = "DUMP_WATCH"
    elif setup_hits >= 1:
        verdict = "DUMP_WATCH"
    else:
        verdict = "PUMP_RISK"

    # Squeeze trap: crowded setup + aggressive taker buy, no fall yet → do not arm entry.
    if (
        taker5 is not None
        and taker5 > 1.12
        and fall < 2.0
        and not trigger
        and not dump.get("confirmed")
    ):
        verdict = "DUMP_WATCH"
    return score, reasons, verdict



DumpHuntTier = Literal["prep", "armed", "likely", "confirmed"]

TIER_RANK = {"prep": 0, "armed": 1, "likely": 2, "confirmed": 3}

# One symbol = max 1 alert per window unless tier escalates (armed→likely→confirmed).
SYMBOL_COOLDOWN_MIN = 45
NEAR_TP1_PCT = 4.0

TIER_BADGE = {
    "prep": "🟠",
    "armed": "🔴",
    "likely": "🚨",
    "confirmed": "🔴",
}

TIER_TITLE = {
    "prep": "DUMP PREP — готовь шорт",
    "armed": "DUMP ARMED — вход близко",
    "likely": "DUMP LIKELY — открывай сделку",
    "confirmed": "DUMP CONFIRMED — вход",
}

TIER_RU = {
    "prep": "Наблюдение",
    "armed": "Скоро",
    "likely": "Готовься",
    "confirmed": "Confirm",
}


def _load_state() -> dict[str, Any]:
    if not DUMP_HUNT_ALERT_STATE.exists():
        return {}
    try:
        payload = json.loads(DUMP_HUNT_ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    DUMP_HUNT_ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    DUMP_HUNT_ALERT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _sym_key(symbol: str) -> str:
    return symbol.strip().upper()


def display_short_setup(
    setup: dict[str, Any],
    *,
    price: float,
    lifecycle: dict[str, Any] | None = None,
    impulse_low: float = 0.0,
    atr15: float = 0.0,
) -> dict[str, Any]:
    """Use hunt setup levels as-is — single TP1 from structural_short_levels."""
    _ = (price, lifecycle, impulse_low, atr15)
    return dict(setup)


def dump_hunt_skip_reason(
    *,
    symbol: str,
    tier: DumpHuntTier,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return block reason or None if alert may send."""
    now = now or datetime.now(UTC)
    sym = _sym_key(symbol)
    lc = lifecycle or {}
    fall = float(lc.get("fall_from_high_pct") or 0)
    phase = str(lc.get("phase") or "")

    leg_tp1 = float(setup.get("leg_tp1") or setup.get("tp1") or 0)
    if leg_tp1 > 0 and price > 0:
        if price <= leg_tp1:
            return "past_leg_tp1"
        dist_pct = (price - leg_tp1) / price * 100.0
        if dist_pct <= NEAR_TP1_PCT and tier in ("prep", "armed"):
            return "near_leg_tp1"

    disp = display_short_setup(setup, price=price, lifecycle=lc)
    disp_tp1 = float(disp.get("tp1") or 0)
    if disp_tp1 > 0 and price <= disp_tp1:
        return "past_display_tp1"

    if phase == "post_dump_bounce" and tier in ("prep", "armed"):
        return "post_dump_bounce"

    state = _load_state()
    sym_state = state.get(sym) if isinstance(state.get(sym), dict) else {}
    last_at_raw = sym_state.get("last_at")
    last_tier = str(sym_state.get("last_tier") or "")
    if last_at_raw:
        try:
            last_at = datetime.fromisoformat(str(last_at_raw))
        except ValueError:
            last_at = None
        if last_at is not None:
            elapsed = now - last_at
            new_rank = TIER_RANK[tier]
            old_rank = TIER_RANK.get(last_tier, -1)  # type: ignore[arg-type]
            if new_rank <= old_rank and elapsed < timedelta(minutes=SYMBOL_COOLDOWN_MIN):
                return "cooldown_same_tier"
            if new_rank < old_rank:
                return "tier_downgrade"
            if new_rank == old_rank and elapsed < timedelta(minutes=SYMBOL_COOLDOWN_MIN):
                return "cooldown_repeat"

    return None


def dump_hunt_cooldown_ok(
    symbol: str,
    tier: DumpHuntTier,
    *,
    now: datetime | None = None,
    price: float = 0.0,
    setup: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> bool:
    if setup is None:
        return True
    return dump_hunt_skip_reason(
        symbol=symbol,
        tier=tier,
        price=price,
        setup=setup,
        lifecycle=lifecycle,
        now=now,
    ) is None


def mark_dump_hunt_sent(
    symbol: str,
    tier: DumpHuntTier,
    *,
    now: datetime | None = None,
    price: float = 0.0,
) -> None:
    now = now or datetime.now(UTC)
    sym = _sym_key(symbol)
    state = _load_state()
    sym_state = dict(state.get(sym) or {})
    sym_state["last_at"] = now.isoformat()
    sym_state["last_tier"] = tier
    sym_state["last_price"] = price
    state[sym] = sym_state
    _save_state(state)


def _fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v >= 100:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _pct_from_entry(entry: float, target: float) -> str:
    if entry <= 0 or target <= 0:
        return ""
    pct = (entry - target) / entry * 100.0
    return f"{pct:.1f}%"


def format_dump_hunt_telegram(
    *,
    symbol: str,
    tier: DumpHuntTier,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    chg_24h: float | None = None,
    dump_init_score: int | None = None,
    dump_reasons: list[str] | None = None,
    note: str = "",
    impulse_low: float = 0.0,
    atr15: float = 0.0,
) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    lc = lifecycle or {}
    disp = display_short_setup(
        setup, price=price, lifecycle=lc, impulse_low=impulse_low, atr15=atr15
    )
    fuel = float(disp.get("dump_fuel") or disp.get("dump_score") or 0)
    score = disp.get("dump_score")
    phase = html.escape(str(disp.get("phase") or "—"))
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    fall = lc.get("fall_from_high_pct")
    fall_txt = f" · fall {float(fall):.1f}%" if fall is not None else ""

    ez = disp.get("entry_zone") or [price, price]
    entry_lo = _fmt_price(ez[0] if len(ez) >= 1 else price)
    entry_hi = _fmt_price(ez[1] if len(ez) >= 2 else price)
    sl = _fmt_price(disp.get("stop_loss"))
    tp1 = disp.get("tp1")
    tp2 = disp.get("tp2")
    tp1_pct = _pct_from_entry(price, float(tp1)) if tp1 else ""
    tp2_pct = _pct_from_entry(price, float(tp2)) if tp2 else ""
    support = disp.get("support_break_level")

    badge = TIER_BADGE[tier]
    tier_ru = TIER_RU.get(tier, tier)
    chg_txt = f" · 24h <code>{chg_24h:.1f}%</code>" if chg_24h is not None else ""
    init_txt = (
        f" · deep score <code>{dump_init_score}</code>"
        if dump_init_score is not None
        else ""
    )

    lines = [
        f"{badge} <b>{tier_ru} · SHORT · {sym}</b>",
        f"Цена <code>{_fmt_price(price)}</code>{chg_txt}{init_txt}{fall_txt}",
        f"Lifecycle <code>{lc_phase}</code> · setup <code>{phase}</code> · fuel <code>{fuel:.0f}</code>"
        + (f" · score <code>{float(score):.0f}</code>" if score is not None else ""),
        f"📍 Вход <code>{entry_lo}–{entry_hi}</code> · SL <code>{sl}</code>",
    ]
    if support:
        lines.append(f"Support break <code>{_fmt_price(support)}</code>")
    if tp1:
        tp1_lbl = disp.get("tp1_label") or "TP1"
        tp1_line = f"🎯 {html.escape(str(tp1_lbl))} <code>{_fmt_price(tp1)}</code>"
        if tp1_pct:
            tp1_line += f" (<b>-{tp1_pct}</b>)"
        if tp2:
            tp2_lbl = disp.get("tp2_label") or "TP2"
            tp2_line = f" · {html.escape(str(tp2_lbl))} <code>{_fmt_price(tp2)}</code>"
            if tp2_pct:
                tp2_line += f" (<b>-{tp2_pct}</b>)"
            tp1_line += tp2_line
        lines.append(tp1_line)

    hard = disp.get("confirm_hard") or []
    if hard:
        lines.append(f"Signals: <code>{html.escape(', '.join(str(h) for h in hard[:6]))}</code>")
    if dump_reasons:
        lines.append(f"Deep: <code>{html.escape(', '.join(dump_reasons[:6]))}</code>")
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    lines.append(f"<i>Этап: {tier_ru} · dump-hunt · не auto-trade</i>")
    return "\n".join(lines)


def tier_from_verdict(verdict: str, *, confirmed: bool) -> DumpHuntTier | None:
    if confirmed:
        return "confirmed"
    v = verdict.upper()
    if v == "DUMP_LIKELY":
        return "likely"
    if v == "DUMP_ARMED":
        return "armed"
    if v == "DUMP_WATCH":
        return "prep"
    return None


async def maybe_send_dump_hunt_telegram(
    broadcaster: Any,
    *,
    symbol: str,
    tier: DumpHuntTier,
    message: str,
    now: datetime | None = None,
    price: float = 0.0,
    setup: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> bool:
    if setup is not None:
        reason = dump_hunt_skip_reason(
            symbol=symbol,
            tier=tier,
            price=price,
            setup=setup,
            lifecycle=lifecycle,
            now=now,
        )
        if reason:
            return False
    result = await broadcaster.send_html(message)
    if result.status != "sent":
        return False
    mark_dump_hunt_sent(symbol, tier, now=now, price=price)
    return True


# Cluster caps prevent correlated triggers (RSI15+RSI1H+div+funding) inflating fuel.
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
            "hidden_div",
            "squeeze_at_boundary",
            "rsi_trendline",
            "macd_div",
            "rejection",
            "bounce",
            "overbought",
            "oversold",
            "wick",
            "at_fib",
            "extended",
            "crowded_long_funding",
            "crowded_short_funding",
            "mom10_",
            "kdj_j_",
            "psy_",
            "ts_rank_",
            "bias_",
            "extreme_move_",
            "volume_spike_",
            "sharpe_",
            "low_sharpe",
            "high_sharpe",
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
            "ema200_confluence",
            "double_bottom_",
            "head_and_shoulders_",
            # POC is the methodology's primary level — aligned POC is structural fuel.
            # Only the aligned form (poc_contra stays raw-only so it can't add + fuel).
            "poc_aligned",
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
            "ws_cvd",
            "ws_depth",
        ),
    ),
    (
        "micro",
        (
            "ws_liq",
            "ws_taker",
            "spot_lead",
            "regime_",
            "bid_wall",
            "ask_wall",
            "zone_imb",
            "volume_regime",
            # Liquidation-heatmap triggers — the dump-hunt edge — now feed micro fuel
            # (previously raw-only → diluted via the ×0.55 blend floor).
            "liq_cluster",
            "liq_cascade",
            "long_squeeze",
            "short_squeeze",
        ),
    ),
)

_WALL_MAX_DISTANCE_PCT = 2.0
_ZONE_IMB_THRESHOLD = 0.15
_WALL_FUEL_SCORE = 6.0
_ZONE_FUEL_SCORE = 4.0
_WS_DEPTH_IMB_THRESHOLD = 0.10
_WS_DEPTH_FUEL = 6.0
_CVD_DIV_PRICE_MIN_PCT = 0.08
_CVD_DIV_FUEL_5M = 10.0
_CVD_DIV_FUEL_1M = 6.0
_STALE_15M_MAX_GAP_MS = 15 * 60 * 1000

_INITIATION_HARD_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
        "5m_rejection_exhaustion",
        "ws_liq_cascade_long_flush",
        "pp_short_break",
    }
)

_INITIATION_HARD_LONG = frozenset(
    {
        "5m_close_above_resistance",
        "15m_close_above_resistance",
        "1m_5m_bull_cascade",
        "5m_bounce_oversold",
        "pp_long_break",
    }
)

_STRUCTURAL_CONFIRM_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
    }
)

_ENTRY_TF_STALE_KEYS = {
    "15m": "stale_15m",
}

# Penalty-only triggers: subtract raw score but must not inflate cluster fuel.
_FUEL_PENALTY_TRIGGERS = frozenset(
    {
        "contra_trend_warning_short",
        "contra_trend_warning_long",
    }
)


def _closed_tf_block(tf: dict[str, Any], interval: str) -> dict[str, Any]:
    key = interval if interval.endswith("_closed") else f"{interval}_closed"
    block = tf.get(key) or {}
    return block if isinstance(block, dict) else {}


def _closed_bar_available(tf: dict[str, Any], interval: str) -> bool:
    return bool(_closed_tf_block(tf, interval).get("closed_bar"))


def _closed_tf_close(tf: dict[str, Any], interval: str) -> float | None:
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return None
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    try:
        if candle.get("close") is not None:
            return float(candle.get("close"))
        raw = block.get("close")
        if raw is None:
            return None
        val = float(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _closed_candle(tf: dict[str, Any], interval: str) -> dict[str, Any]:
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return {}
    candle = block.get("candle")
    return candle if isinstance(candle, dict) else {}


def _required_closed_rsi(tf: dict[str, Any], interval: str) -> float | None:
    """RSI from closed frame only — None when missing (no silent default)."""
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return None
    raw = block.get("rsi14")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _entry_tf_stale(tf: dict[str, Any], interval: str) -> bool:
    stale_key = _ENTRY_TF_STALE_KEYS.get(interval)
    return bool(tf.get(stale_key)) if stale_key else False


def _structural_close_break_triggers(
    *,
    direction: Literal["long", "short"],
    level: float,
    tf: dict[str, Any],
    entry_tf: str,
) -> list[str]:
    """Closed-bar structural breaks on entry_confirm_tf (+ independent 15m secondary)."""
    hard: list[str] = []
    if level <= 0:
        return hard
    # Primary entry-TF break — gated only on its OWN bar availability.
    entry_close = _closed_tf_close(tf, entry_tf)
    if entry_close is not None and entry_close > 0 and not _entry_tf_stale(tf, entry_tf):
        if direction == "short" and entry_close < level:
            hard.append(f"{entry_tf}_close_below_support")
        elif direction == "long" and entry_close > level:
            hard.append(f"{entry_tf}_close_above_resistance")
    # 15m secondary is INDEPENDENT of the (faster) primary bar — a missing/stale 1m
    # bar must not drop the 15m structural confirm (dump entry_tf=1m regression).
    if entry_tf != "15m":
        r15_close = _closed_tf_close(tf, "15m")
        if direction == "short" and r15_close and r15_close < level and not tf.get("stale_15m"):
            hard.append("15m_close_below_support")
        elif direction == "long" and r15_close and r15_close > level and not tf.get("stale_15m"):
            hard.append("15m_close_above_resistance")
    return hard


def _is_structural_confirm_trigger(trigger: str) -> bool:
    t = str(trigger)
    if t.endswith("_score_only"):
        return False
    if "cascade" in t:
        return True
    return "close_below_support" in t or "close_above_resistance" in t or t in {
        "pp_short_break",
        "pp_long_break",
    }


def _resolve_lifecycle_4h(setup: dict[str, Any]) -> str:
    direct = setup.get("lifecycle_4h") or setup.get("phase_4h")
    if direct:
        return str(direct)
    lc = setup.get("lifecycle")
    if isinstance(lc, dict):
        return str(lc.get("lifecycle_4h") or lc.get("phase_4h") or "")
    return ""

_HIDDEN_DIV_FUEL = 10.0
_CHART_PATTERN_FUEL = 5.0
_PROKOL_FUEL_PENALTY = 8.0
_PROKOL_TF_TRAP_PENALTY = 12.0
_HIDDEN_DIV_TFS = frozenset({"1h", "4h"})
_CHART_PATTERN_TFS = frozenset({"1h", "4h"})
_POLARS_TA_TF_KEYS = ("15m_closed", "15m", "1h", "4h")
_MOM_ALIGNED_FUEL = 4.0
_KDJ_EXHAUST_FUEL = 6.0
_KDJ_POST_DUMP_FUEL = 6.0
_PSY_EUPHORIA_FUEL = 8.0
_PSY_PANIC_FUEL = 8.0
_TS_RANK_EXHAUST_FUEL = 4.0
_BIAS_EXTREME_FUEL = 6.0
_EXTREME_MOVE_Z = 2.5
_EXTREME_MOVE_FUEL = 8.0
_VOLUME_SPIKE_PERCENTILE = 95.0
_VOLUME_SPIKE_FUEL = 8.0
_CONTRA_TREND_PENALTY = 5.0
_CONTRA_TREND_SLOPE_MIN = 0.05
_SHARPE_PHASE_FUEL = 4.0
_SHARPE_LOW_THRESHOLD = 0.0
_SHARPE_HIGH_THRESHOLD = 1.0
_VOLUME_REGIME_BREAK_FUEL = 5.0
_CANDLE_REVERSAL_FUEL = 6.0
_CANDLE_STAR_FUEL = 10.0
_CANDLE_LEVEL_PCT = 0.02
_CANDLE_TF_KEYS = ("15m_closed", "5m_closed")
_EXHAUSTION_PHASES = frozenset(
    {
        "exhaustion_at_high",
        "exhaustion_watch",
        "distribution",
    }
)
_POST_DUMP_PHASES = frozenset(
    {
        "post_dump_bounce",
        "recovery",
        "accumulation",
    }
)


def _resolve_ema200(setup: dict[str, Any], tf: dict[str, Any] | None) -> float:
    direct = setup.get("ema200")
    if direct is not None:
        try:
            val = float(direct)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return 0.0
    for key in ("1h", "4h", "15m_closed", "15m"):
        block = blocks.get(key) or {}
        if not isinstance(block, dict):
            continue
        raw = block.get("ema200")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0.0


def ema200_confluence_trigger(
    *,
    direction: str,
    price: float,
    ema200: float,
    symbol: str = "",
) -> str | None:
    """+8 fuel when price hugs EMA200 in the setup direction (Phase 5A)."""
    sc = scoring_thresholds(symbol)
    confluence_pct = float(sc.get("ema200_confluence_pct", 0.005))
    if price <= 0 or ema200 <= 0:
        return None
    if abs(price - ema200) / price >= confluence_pct:
        return None
    if direction == "long" and price >= ema200:
        return "ema200_confluence_support"
    if direction == "short" and price <= ema200:
        return "ema200_confluence_resistance"
    return None


def _htf_blocks(tf: dict[str, Any] | None, keys: frozenset[str]) -> list[tuple[str, dict[str, Any]]]:
    blocks = tf or {}
    if not isinstance(blocks, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for key in keys:
        block = blocks.get(key) or {}
        if isinstance(block, dict) and block.get("status") != "empty":
            out.append((key, block))
    return out


def _price_at_structure_boundary(
    block: dict[str, Any],
    *,
    direction: str,
    symbol: str = "",
) -> bool:
    """Squeeze compression at BB/Donchian edge (Phase 5B)."""
    sc = scoring_thresholds(symbol)
    bb_boundary = float(sc.get("squeeze_bb_boundary", 0.08))
    bb = block.get("bb_pct_b")
    if bb is not None:
        bb_f = float(bb)
        if direction == "long" and bb_f <= bb_boundary:
            return True
        if direction == "short" and bb_f >= 1.0 - bb_boundary:
            return True
    close = float(block.get("close") or 0)
    if close <= 0:
        return False
    tol = close * 0.003
    d_hi = block.get("donchian_high20")
    d_lo = block.get("donchian_low20")
    if direction == "long" and d_lo is not None:
        return abs(close - float(d_lo)) <= tol
    if direction == "short" and d_hi is not None:
        return abs(close - float(d_hi)) <= tol
    return False


def squeeze_at_boundary_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
    symbol: str = "",
) -> str | None:
    """+8 fuel when TTM squeeze is on and price sits at a structure boundary."""
    for tf_key, block in _htf_blocks(tf, _HIDDEN_DIV_TFS):
        if not block.get("squeeze_on"):
            continue
        if _price_at_structure_boundary(block, direction=direction, symbol=symbol):
            return f"squeeze_at_boundary_{tf_key}"
    return None


def hidden_div_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
) -> str | None:
    """+10 fuel on hidden Stoch divergence (1h/4h only, Phase 5C)."""
    flag = "bullish_hidden_stoch_div" if direction == "long" else "bearish_hidden_stoch_div"
    for tf_key, block in _htf_blocks(tf, _HIDDEN_DIV_TFS):
        if block.get(flag):
            return f"hidden_div_{tf_key}"
    return None


def chart_pattern_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
) -> list[str]:
    """+5 fuel per aligned HTF chart pattern (1h/4h only, Phase 6A)."""
    out: list[str] = []
    for tf_key, block in _htf_blocks(tf, _CHART_PATTERN_TFS):
        if direction == "long":
            pat = block.get("double_bottom")
            if isinstance(pat, dict) and pat.get("pattern") == "double_bottom":
                out.append(f"double_bottom_{tf_key}")
        elif direction == "short":
            pat = block.get("head_and_shoulders")
            if isinstance(pat, dict) and pat.get("pattern") == "head_and_shoulders":
                out.append(f"head_and_shoulders_{tf_key}")
    return out


def _apply_fuel_trigger(
    setup: dict[str, Any],
    *,
    score_key: str,
    trigger: str | None,
    fuel: float,
) -> None:
    if not trigger:
        return
    triggers = list(setup.get("triggers") or [])
    if trigger in triggers:
        return
    triggers.append(trigger)
    setup["triggers"] = triggers
    setup[score_key] = round(float(setup.get(score_key) or 0) + fuel, 1)


def _apply_squeeze_at_boundary(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
    symbol: str = "",
) -> None:
    sc = scoring_thresholds(symbol)
    _apply_fuel_trigger(
        setup,
        score_key=score_key,
        trigger=squeeze_at_boundary_trigger(direction=direction, tf=tf, symbol=symbol),
        fuel=float(sc.get("squeeze_boundary_fuel", 8.0)),
    )


def _apply_hidden_div_fuel(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    _apply_fuel_trigger(
        setup,
        score_key=score_key,
        trigger=hidden_div_trigger(direction=direction, tf=tf),
        fuel=_HIDDEN_DIV_FUEL,
    )


def _apply_chart_pattern_fuel(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger in chart_pattern_trigger(direction=direction, tf=tf):
        _apply_fuel_trigger(
            setup,
            score_key=score_key,
            trigger=trigger,
            fuel=_CHART_PATTERN_FUEL,
        )


def _resolve_tf_indicator(
    setup: dict[str, Any],
    tf: dict[str, Any] | None,
    key: str,
) -> float | None:
    """Read polars-ta column from setup dict or TF snapshot blocks (Phase 8A)."""
    direct = setup.get(key)
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return None
    for tf_key in _POLARS_TA_TF_KEYS:
        block = blocks.get(tf_key) or {}
        if not isinstance(block, dict) or block.get("status") == "empty":
            continue
        raw = block.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_kdj_j(setup: dict[str, Any], tf: dict[str, Any] | None) -> float | None:
    j = _resolve_tf_indicator(setup, tf, "kdj_j14")
    if j is not None:
        return j
    k = _resolve_tf_indicator(setup, tf, "kdj_k14")
    d = _resolve_tf_indicator(setup, tf, "kdj_d14")
    if k is None or d is None:
        return None
    return 3.0 * k - 2.0 * d


def _lifecycle_phase(setup: dict[str, Any]) -> str:
    return str(setup.get("lifecycle_phase") or setup.get("phase") or "")


def polars_ta_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """polars-ta / pinned-panel fuel overlays when columns are present (Phase 8A)."""
    out: list[tuple[str, float]] = []
    mom = _resolve_tf_indicator(setup, tf, "mom10")
    if mom is not None:
        if direction == "short" and mom < 0:
            out.append(("mom10_bear_aligned", _MOM_ALIGNED_FUEL))
        elif direction == "long" and mom > 0:
            out.append(("mom10_bull_aligned", _MOM_ALIGNED_FUEL))

    kdj_j = _resolve_kdj_j(setup, tf)
    phase = _lifecycle_phase(setup)
    if kdj_j is not None:
        if direction == "short" and kdj_j > 100.0:
            out.append(("kdj_j_exhaustion_short", _KDJ_EXHAUST_FUEL))
        elif direction == "long" and kdj_j < 0.0 and phase in _POST_DUMP_PHASES:
            out.append(("kdj_j_post_dump_long", _KDJ_POST_DUMP_FUEL))

    psy = _resolve_tf_indicator(setup, tf, "psy12")
    if psy is not None:
        if direction == "short" and psy > 83.0:
            out.append(("psy_euphoria_short", _PSY_EUPHORIA_FUEL))
        elif direction == "long" and psy < 17.0:
            out.append(("psy_panic_long", _PSY_PANIC_FUEL))

    ts_rank = _resolve_tf_indicator(setup, tf, "wq_ts_rank_close20")
    if (
        direction == "short"
        and ts_rank is not None
        and ts_rank > 0.95
        and phase in _EXHAUSTION_PHASES
    ):
        out.append(("ts_rank_exhaustion_top", _TS_RANK_EXHAUST_FUEL))

    bias = _resolve_tf_indicator(setup, tf, "bias6")
    if bias is not None:
        if direction == "short" and bias > 8.0:
            out.append(("bias_overbought_short", _BIAS_EXTREME_FUEL))
        elif direction == "long" and bias < -8.0:
            out.append(("bias_oversold_long", _BIAS_EXTREME_FUEL))

    return out


def research_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """polars-ols / polars-trading / polars-ds fuel overlays (Phases 11A–11C)."""
    out: list[tuple[str, float]] = []
    slope = _resolve_tf_indicator(setup, tf, "trend_slope_20")
    if slope is not None:
        if direction == "short" and slope > _CONTRA_TREND_SLOPE_MIN:
            out.append(("contra_trend_warning_short", -_CONTRA_TREND_PENALTY))
        elif direction == "long" and slope < -_CONTRA_TREND_SLOPE_MIN:
            out.append(("contra_trend_warning_long", -_CONTRA_TREND_PENALTY))

    sharpe = _resolve_tf_indicator(setup, tf, "sharpe_20")
    phase = _lifecycle_phase(setup)
    if sharpe is not None:
        if (
            direction == "short"
            and phase in _EXHAUSTION_PHASES
            and sharpe < _SHARPE_LOW_THRESHOLD
        ):
            out.append(("low_sharpe_exhaustion_short", _SHARPE_PHASE_FUEL))
        if (
            direction == "long"
            and phase in _POST_DUMP_PHASES
            and sharpe > _SHARPE_HIGH_THRESHOLD
        ):
            out.append(("high_sharpe_accumulation_long", _SHARPE_PHASE_FUEL))

    if setup.get("volume_regime_break"):
        out.append(("volume_regime_break", _VOLUME_REGIME_BREAK_FUEL))

    return out


def _apply_research_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in research_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def distribution_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """Return distribution + volume percentile fuel overlays (Phase 12A)."""
    out: list[tuple[str, float]] = []
    zscore = _resolve_tf_indicator(setup, tf, "return_zscore")
    phase = _lifecycle_phase(setup)
    if zscore is not None and abs(zscore) > _EXTREME_MOVE_Z:
        if direction == "short":
            if phase in _EXHAUSTION_PHASES and zscore > 0:
                out.append(("extreme_move_bear", _EXTREME_MOVE_FUEL))
            elif zscore < 0:
                out.append(("extreme_move_bear_cont", _EXTREME_MOVE_FUEL))
        elif direction == "long" and zscore > 0:
            out.append(("extreme_move_bull", _EXTREME_MOVE_FUEL))
        elif direction == "long" and zscore < 0 and phase in {
            "post_dump_bounce",
            "recovery",
            "accumulation",
        }:
            out.append(("extreme_move_bull_bounce", _EXTREME_MOVE_FUEL))

    vol_pct = _resolve_tf_indicator(setup, tf, "volume_percentile")
    if vol_pct is not None and vol_pct > _VOLUME_SPIKE_PERCENTILE:
        out.append(("volume_spike_percentile", _VOLUME_SPIKE_FUEL))

    return out


def _apply_distribution_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in distribution_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _apply_polars_ta_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in polars_ta_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _near_price_level(price: float, level: float, *, pct: float = _CANDLE_LEVEL_PCT) -> bool:
    if price <= 0 or level <= 0:
        return False
    return abs(price - level) / level <= pct


def _near_support(price: float, setup: dict[str, Any]) -> bool:
    for key in ("local_support", "impulse_low", "support_break_level", "invalidation_below"):
        lvl = float(setup.get(key) or 0)
        if _near_price_level(price, lvl):
            return True
    return False


def _near_resistance(price: float, setup: dict[str, Any]) -> bool:
    for key in ("local_resistance", "impulse_high", "resistance_break_level", "invalidation_above"):
        lvl = float(setup.get(key) or 0)
        if _near_price_level(price, lvl):
            return True
    return False


def _candle_block(tf: dict[str, Any], tf_key: str) -> dict[str, Any]:
    block = tf.get(tf_key) or {}
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    return candle if isinstance(candle, dict) else {}


def _candle_flag(candle: dict[str, Any], key: str) -> bool:
    raw = candle.get(key)
    if raw is None:
        return False
    try:
        return bool(float(raw) >= 0.5)
    except (TypeError, ValueError):
        return bool(raw)


def candle_pattern_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> list[tuple[str, float]]:
    """Hammer/shooting-star/star pattern fuel overlays (Phase 8B)."""
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return []
    px = price or float(setup.get("price") or 0)
    out: list[tuple[str, float]] = []
    for tf_key in _CANDLE_TF_KEYS:
        candle = _candle_block(blocks, tf_key)
        tag = tf_key.removesuffix("_closed")
        if direction == "long":
            if _candle_flag(candle, "candle_hammer") and _near_support(px, setup):
                out.append((f"hammer_at_support_{tag}", _CANDLE_REVERSAL_FUEL))
            if _candle_flag(candle, "candle_morning_star"):
                out.append((f"morning_star_{tag}", _CANDLE_STAR_FUEL))
        else:
            if _candle_flag(candle, "candle_shooting_star") and _near_resistance(px, setup):
                out.append((f"shooting_star_at_resistance_{tag}", _CANDLE_REVERSAL_FUEL))
            if _candle_flag(candle, "candle_evening_star"):
                out.append((f"evening_star_{tag}", _CANDLE_STAR_FUEL))
    return out


def candle_pattern_hard_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> list[str]:
    """Engulfing at structure as optional hard confirm (Phase 8B)."""
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return []
    px = price or float(setup.get("price") or 0)
    hard: list[str] = []
    for tf_key in _CANDLE_TF_KEYS:
        candle = _candle_block(blocks, tf_key)
        tag = tf_key.removesuffix("_closed")
        if direction == "long" and _candle_flag(candle, "candle_bullish_engulfing"):
            if _near_support(px, setup):
                hard.append(f"{tag}_bullish_engulfing_at_support")
        elif direction == "short" and _candle_flag(candle, "candle_bearish_engulfing"):
            if _near_resistance(px, setup):
                hard.append(f"{tag}_bearish_engulfing_at_resistance")
    return hard


def _apply_candle_pattern_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> None:
    for trigger, fuel in candle_pattern_fuel_triggers(
        setup, direction=direction, tf=tf, price=price
    ):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _apply_ema200_confluence(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    price: float = 0.0,
    tf: dict[str, Any] | None = None,
    symbol: str = "",
) -> None:
    sc = scoring_thresholds(symbol)
    px = price or float(setup.get("price") or 0)
    ema200 = _resolve_ema200(setup, tf)
    trig = ema200_confluence_trigger(direction=direction, price=px, ema200=ema200, symbol=symbol)
    if not trig:
        return
    triggers = list(setup.get("triggers") or [])
    if trig in triggers:
        return
    triggers.append(trig)
    setup["triggers"] = triggers
    setup[score_key] = round(float(setup.get(score_key) or 0) + float(sc.get("ema200_fuel", 8.0)), 1)


def _apply_prokol_fuel_penalty(
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any] | None,
    level: float,
) -> None:
    """Tag prokol trap on setup; fuel adjustment is applied via compute_setup_fuel."""
    from hunt_core.gate.delivery import detect_prokol

    if level <= 0 or not tf:
        return

    trap = detect_prokol(level=level, break_direction=direction, tf=tf)
    if not trap.get("prokol"):
        return
    triggers = list(setup.get("triggers") or [])
    tag = f"prokol_trap_{direction}"
    if tag not in triggers:
        triggers.append(tag)
    setup["triggers"] = triggers
    setup["prokol_trap"] = trap


def long_resistance_chase_veto(
    resistance: float,
    price: float,
    r5_close: float,
) -> bool:
    """Veto late long chase; allow 0.5% retest when 5m closed above resistance."""
    if resistance <= 0:
        return False
    if price <= 0:
        return False
    ratio = 0.995 if r5_close > resistance else 0.998
    return price < resistance * ratio


def _wall_dict(market: dict[str, Any], side: str) -> dict[str, Any] | None:
    key = "nearest_bid_wall" if side == "bid" else "nearest_ask_wall"
    raw = market.get(key)
    return raw if isinstance(raw, dict) else None


def _resolve_depth_imbalance(market: dict[str, Any]) -> float | None:
    """Prefer WS top-20 depth imbalance over REST/L1 book fields (Phase 7C)."""
    ws = market.get("ws_depth_imbalance")
    if ws is not None:
        try:
            return float(ws)
        except (TypeError, ValueError):
            pass
    rest = market.get("depth_imbalance")
    if rest is not None:
        try:
            return float(rest)
        except (TypeError, ValueError):
            pass
    live = market.get("live_depth_imbalance")
    if live is not None:
        try:
            return float(live)
        except (TypeError, ValueError):
            pass
    return None


def ws_depth_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
) -> list[tuple[str, float]]:
    """WS top-20 depth imbalance fuel (Phase 7C)."""
    imb = _resolve_depth_imbalance(market)
    if imb is None:
        return []
    if direction == "short" and imb <= -_WS_DEPTH_IMB_THRESHOLD:
        return [("ws_depth_ask_heavy", _WS_DEPTH_FUEL)]
    if direction == "long" and imb >= _WS_DEPTH_IMB_THRESHOLD:
        return [("ws_depth_bid_heavy", _WS_DEPTH_FUEL)]
    return []


def ws_cvd_divergence_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
) -> list[tuple[str, float]]:
    """CVD vs price divergence from WS agg trades (Phase 7D)."""
    out: list[tuple[str, float]] = []
    windows = (
        ("5m", "ws_cvd_5m", "ws_price_chg_5m", _CVD_DIV_FUEL_5M),
        ("1m", "ws_cvd_1m", "ws_price_chg_1m", _CVD_DIV_FUEL_1M),
    )
    for label, cvd_key, px_key, fuel in windows:
        cvd_raw = market.get(cvd_key)
        px_raw = market.get(px_key)
        if cvd_raw is None or px_raw is None:
            continue
        try:
            cvd = float(cvd_raw)
            px_chg = float(px_raw)
        except (TypeError, ValueError):
            continue
        if direction == "short" and px_chg >= _CVD_DIV_PRICE_MIN_PCT and cvd < 0.0:
            out.append((f"ws_cvd_bear_div_{label}", fuel))
        elif direction == "long" and px_chg <= -_CVD_DIV_PRICE_MIN_PCT and cvd > 0.0:
            out.append((f"ws_cvd_bull_div_{label}", fuel))
    return out


def _apply_ws_orderflow_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    market: dict[str, Any] | None,
) -> None:
    mkt = market or {}
    for trigger, fuel in (
        *ws_depth_fuel_triggers(mkt, direction=direction),
        *ws_cvd_divergence_fuel_triggers(mkt, direction=direction),
    ):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def wall_depth_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    price: float = 0.0,
    symbol: str = "",
) -> tuple[float, list[str]]:
    """Book wall + zone-imbalance fuel overlays (Phase 2B/2C)."""

    sc = scoring_thresholds(symbol)
    wall_dist = float(sc.get("wall_max_distance_pct", 2.0))
    wall_fuel = float(sc.get("wall_fuel_score", 6.0))
    zone_thresh = float(sc.get("zone_imb_threshold", 0.15))
    zone_fuel = float(sc.get("zone_fuel_score", 4.0))
    score = 0.0
    triggers: list[str] = []
    bid_wall = _wall_dict(market, "bid")
    ask_wall = _wall_dict(market, "ask")
    zone = market.get("depth_zone_imbalance")
    zone_map = zone if isinstance(zone, dict) else {}
    mark: float | None = None
    try:
        mark = require_mark_price(price, market)
    except Exception:
        mark = optional_finite_float(price) or optional_finite_float((market or {}).get("last_price"))
    if mark is None:
        return score, triggers

    if direction == "long" and bid_wall:
        dist = float(bid_wall.get("distance_pct") or 999.0)
        sig = float(bid_wall.get("significance_pct") or 0.0)
        px = float(bid_wall.get("price_center") or 0.0)
        below = mark <= 0 or px <= mark
        if sig >= 0.5 and dist <= wall_dist and below:
            score += wall_fuel
            triggers.append("bid_wall_support")

    if direction == "short" and ask_wall:
        dist = float(ask_wall.get("distance_pct") or 999.0)
        sig = float(ask_wall.get("significance_pct") or 0.0)
        px = float(ask_wall.get("price_center") or 0.0)
        above = mark <= 0 or px >= mark
        if sig >= 0.5 and dist <= wall_dist and above:
            score += wall_fuel
            triggers.append("ask_wall_resistance")

    best_band: str | None = None
    best_mag = 0.0
    for band, imb in zone_map.items():
        try:
            val = float(imb)
        except (TypeError, ValueError):
            continue
        if direction == "long" and val >= zone_thresh and val > best_mag:
            best_mag = val
            best_band = str(band)
        elif direction == "short" and val <= -zone_thresh and abs(val) > best_mag:
            best_mag = abs(val)
            best_band = str(band)
    if best_band is not None:
        score += zone_fuel
        tag = "bid_heavy" if direction == "long" else "ask_heavy"
        triggers.append(f"zone_imb_{tag}_{best_band}")

    return score, triggers


def _cluster_for_trigger(trigger: str) -> str | None:
    t = str(trigger).lower()
    for cluster, needles in _FUEL_CLUSTER_RULES:
        if any(n in t for n in needles):
            return cluster
    return None


def cluster_fuel(triggers: list[str], *, raw_score: float, symbol: str = "") -> float:
    """Deduplicated fuel: sum of per-cluster contributions, each capped."""
    sc = scoring_thresholds(symbol)
    cap = float(sc.get("cluster_cap", 28.0))
    w_default = float(sc.get("trigger_weight_default", 12.0))
    w_structure = float(sc.get("trigger_weight_structure", 28.0))
    w_close = float(sc.get("trigger_weight_close_break", 22.0))
    w_div = float(sc.get("trigger_weight_div", 18.0))
    w_trend = float(sc.get("trigger_weight_trendline", 8.0))
    w_reject = float(sc.get("trigger_weight_rejection", 16.0))
    blend = float(sc.get("fuel_raw_blend_ratio", 0.55))
    buckets: dict[str, float] = {c: 0.0 for c, _ in _FUEL_CLUSTER_RULES}
    for trig in triggers:
        if str(trig) in _FUEL_PENALTY_TRIGGERS:
            continue
        cluster = _cluster_for_trigger(trig)
        if cluster is None:
            continue
        w = w_default
        if "lost_support" in trig or "broke_resistance" in trig:
            w = w_structure
        elif "close_below" in trig or "close_above" in trig or "cascade" in trig:
            w = w_close
        elif "div" in trig:
            w = w_div
        elif "trendline" in trig:
            w = w_trend
        elif "rejection" in trig or "bounce" in trig:
            w = w_reject
        buckets[cluster] = min(cap, buckets[cluster] + w)
    fuel = sum(buckets.values())
    return round(min(100.0, max(fuel, min(raw_score * blend, 100.0))), 1)


def compute_setup_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    symbol: str = "",
    tf: dict[str, Any] | None = None,
) -> float:
    """Cluster fuel + prokol penalty — must match enrich_dump/long_setup output."""
    score_key = "dump_score" if direction == "short" else "long_score"
    triggers = list(setup.get("triggers") or [])
    raw = float(setup.get(score_key) or 0)
    fuel = cluster_fuel(triggers, raw_score=raw, symbol=symbol)
    if not tf:
        return fuel
    if direction == "short":
        level = float(setup.get("support_break_level") or setup.get("local_support") or 0)
    else:
        level = float(
            setup.get("resistance_break_level") or setup.get("local_resistance") or 0
        )
    if level <= 0:
        return fuel

    from hunt_core.gate.delivery import detect_prokol

    trap = detect_prokol(level=level, break_direction=direction, tf=tf)
    if not trap.get("prokol"):
        return fuel
    penalty = _PROKOL_TF_TRAP_PENALTY if trap.get("tf_trap") else _PROKOL_FUEL_PENALTY
    return round(max(0.0, fuel - penalty), 1)


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
        return False, "orderflow_data_invalid"
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
    r5_close = _closed_tf_close(tf, "5m")
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
    if lifecycle_bias in {"short", "wait"} and lc_phase not in _LONG_PUMP_PHASES:
        veto = "veto_lifecycle_bias_short" if lifecycle_bias == "short" else "veto_lifecycle_bias_wait"
        return False, [veto]
    phase_4h = _resolve_lifecycle_4h(long_setup)
    blocked_htf, htf_reason = _htf_bias_override(phase_4h, "long")
    if blocked_htf:
        return False, [f"veto_{htf_reason}"]
    mkt = market or {}
    hard: list[str] = []
    resistance = long_setup.get("resistance_break_level") or 0.0
    c1 = _closed_candle(tf, "1m")
    c5 = _closed_candle(tf, "5m")
    r5_close = _closed_tf_close(tf, "5m")
    r15_rsi = _required_closed_rsi(tf, "15m")
    if r15_rsi is None:
        return False, ["veto_data_missing_rsi15m"]
    entry_tf = entry_confirm_tf(symbol, direction="long")
    hard.extend(
        _structural_close_break_triggers(
            direction="long",
            level=float(resistance or 0),
            tf=tf,
            entry_tf=entry_tf,
        )
    )
    lc = long_setup.get("lifecycle") if isinstance(long_setup.get("lifecycle"), dict) else {}
    bounce_pct = float(lc.get("bounce_from_low_pct") or long_setup.get("bounce_from_low_pct") or 0)
    from hunt_core.gate.policy import mtf_confirm_veto  # noqa: PLC0415

    blocked, mtf_reason = mtf_confirm_veto(
        "long",
        tf,
        lc_phase,
        market=mkt,
        fall_from_high_pct=float(long_setup.get("fall_from_high_pct") or 0),
        bounce_from_low_pct=bounce_pct,
    )
    if blocked:
        return False, [f"veto_{mtf_reason}"]
    if long_setup.get("level_expired"):
        from hunt_core.gate.policy import check_mtf_structure_break  # noqa: PLC0415

        allowed, sb_reason = check_mtf_structure_break("long", tf, level_expired=True)
        if not allowed:
            return False, [f"veto_{sb_reason}"]

    if long_resistance_chase_veto(
        resistance, float(price or 0) or r5_close, r5_close
    ):
        return False, ["veto_price_below_resistance"]
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35 and r15_rsi <= 40:
        hard.append("5m_bounce_oversold")
    if c1.get("bullish") and c5.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bull_cascade")
    r15_closed = _closed_tf_block(tf, "15m")
    r1h_closed = _closed_tf_block(tf, "1h")
    if r15_closed.get("closed_bar") and r15_closed.get("pp_long_true"):
        hard.append("pp_long_break")
    elif r1h_closed.get("closed_bar") and r1h_closed.get("pp_long_true"):
        hard.append("pp_long_break")
    hard.extend(
        candle_pattern_hard_triggers(
            long_setup, direction="long", tf=tf, price=float(price or 0)
        )
    )

    fuel = float(long_setup.get("long_fuel") or 0)
    r4h_closed = _closed_tf_block(tf, "4h")
    div = (
        r1h_closed.get("bullish_rsi_div")
        or r4h_closed.get("bullish_rsi_div")
        or r1h_closed.get("bullish_macd_div")
        or r4h_closed.get("bullish_macd_div")
    )
    triggers = long_setup.get("triggers") or []
    structural = [
        h
        for h in hard
        if _is_structural_confirm_trigger(h) or h in {"pp_long_break", "5m_bounce_oversold"} or "engulfing" in h
    ]
    secondary = sum(
        1
        for cond in (
            bool(div),
            "oi_build" in triggers,
            any(str(t).startswith("broke_resistance") for t in triggers),
            any("ws_taker_buy" in str(t) for t in triggers),
            any("spot_lead_pump" in str(t) for t in triggers),
            lc_phase in {"impulse_initiating", "breakout_arming"},
            (lambda imb: isinstance(imb, (int, float)) and float(imb) >= 0.10)(
                _resolve_depth_imbalance(mkt)
            ),
        )
        if cond
    )
    closed_break = any("close_above_resistance" in h for h in structural)
    chg24 = float(long_setup.get("context_chg_24h_pct") or 0)
    pos_raw = long_setup.get("context_pos_in_range")
    if pos_raw is None:
        return False, ["veto_data_missing_pos_in_range"]
    try:
        pos_rng = float(pos_raw)
    except (TypeError, ValueError):
        return False, ["veto_data_missing_pos_in_range"]
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
    if has_initiation and fuel >= cal.confirm_min_score:
        return "dump_imminent"
    if has_initiation and fuel >= cal.forming_min_score:
        return "dump_initiating"
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
    if has_initiation and fuel >= cal.confirm_min_score:
        return "long_imminent"
    if has_initiation and fuel >= cal.forming_min_score:
        return "long_initiating"
    if fuel >= cal.forming_min_score:
        return "long_setup_forming"
    if fuel >= 25:
        return "accumulation_watch"
    return "no_long_yet"


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


def enrich_long_setup(
    setup: dict[str, Any],
    *,
    price: float = 0.0,
    tf: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sym = str(setup.get("symbol") or "")
    sc = scoring_thresholds(sym)
    _apply_ema200_confluence(
        setup, direction="long", score_key="long_score", price=price, tf=tf, symbol=sym
    )
    _apply_squeeze_at_boundary(
        setup, direction="long", score_key="long_score", tf=tf, symbol=sym
    )
    _apply_hidden_div_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_chart_pattern_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_polars_ta_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_distribution_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_research_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_candle_pattern_fuel(
        setup, direction="long", score_key="long_score", tf=tf, price=price
    )
    _apply_ws_orderflow_fuel(setup, direction="long", score_key="long_score", market=market)
    level = float(
        setup.get("resistance_break_level")
        or setup.get("local_resistance")
        or 0
    )
    _apply_prokol_fuel_penalty(
        setup, direction="long", tf=tf, level=level
    )
    setup["long_fuel"] = compute_setup_fuel(setup, direction="long", symbol=sym, tf=tf)
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
        setup["long_fuel"] = round(
            min(float(setup["long_fuel"]), float(sc.get("accumulation_long_fuel_cap", 72.0))),
            1,
        )
    return setup



STATIC_IGNITION_MIN_PCT = 2.5
STATIC_RANGE_HOT_PCT = 8.0
STATIC_PUMP_EXTREME_PCT = 15.0
EWMA_ALPHA = 0.08
MIN_TICK_SAMPLES = 6
MIN_CHANGE_SAMPLES = 4
Z_IGNITION = 2.0
Z_RANGE_HOT = 1.8
Z_PUMP_EXTREME = 2.5
TICK_FLOOR_PCT = 0.8
VAR_FLOOR = 0.05


def _sigma(var: float) -> float:
    return float(pl.Series([max(var, VAR_FLOOR)]).sqrt()[0])


@dataclass(slots=True)
class SymbolAdaptive:
    symbol: str
    tick_mu: float = 0.0
    tick_var: float = 1.0
    tick_n: int = 0
    chg_mu: float = 5.0
    chg_var: float = 16.0
    chg_n: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_n": self.tick_n,
            "tick_mu_abs_pct": round(self.tick_mu, 3),
            "tick_sigma_pct": round(_sigma(self.tick_var), 3),
            "chg_n": self.chg_n,
            "chg_mu_abs_pct": round(self.chg_mu, 2),
            "chg_sigma_pct": round(_sigma(self.chg_var), 2),
            "adaptive_ready": self.tick_n >= MIN_TICK_SAMPLES or self.chg_n >= MIN_CHANGE_SAMPLES,
        }


@dataclass(slots=True)
class AdaptiveStore:
    symbols: dict[str, SymbolAdaptive] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdaptiveStore:
        symbols: dict[str, SymbolAdaptive] = {}
        for sym, item in (raw.get("symbols") or {}).items():
            if not isinstance(item, dict):
                continue
            symbols[str(sym).upper()] = SymbolAdaptive(
                symbol=str(sym).upper(),
                tick_mu=float(item.get("tick_mu") or 0),
                tick_var=float(item.get("tick_var") or 1),
                tick_n=int(item.get("tick_n") or 0),
                chg_mu=float(item.get("chg_mu") or 5),
                chg_var=float(item.get("chg_var") or 16),
                chg_n=int(item.get("chg_n") or 0),
            )
        return cls(symbols=symbols)

    def to_dict(self) -> dict[str, Any]:
        return {"symbols": {sym: asdict(st) for sym, st in self.symbols.items()}}


def _read_ewma_raw(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def load_adaptive_store(path: Path = EWMA_THRESHOLDS) -> AdaptiveStore:
    for candidate in (path, EWMA_THRESHOLDS, ADAPTIVE_THRESHOLDS):
        raw = _read_ewma_raw(candidate)
        if raw is None:
            continue
        if "symbols" in raw:
            return AdaptiveStore.from_dict(raw)
    return AdaptiveStore()


def save_adaptive_store(store: AdaptiveStore, path: Path = EWMA_THRESHOLDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
    if path != ADAPTIVE_THRESHOLDS and ADAPTIVE_THRESHOLDS.exists():
        try:
            legacy = _read_ewma_raw(ADAPTIVE_THRESHOLDS) or {}
            if any(k in legacy for k in ("universal", "per_symbol", "outcome_calibration")):
                ADAPTIVE_THRESHOLDS.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass


def _sym(store: AdaptiveStore, symbol: str) -> SymbolAdaptive:
    sym = symbol.upper()
    if sym not in store.symbols:
        store.symbols[sym] = SymbolAdaptive(symbol=sym)
    return store.symbols[sym]


def ewma_update(mean: float, var: float, value: float, *, alpha: float = EWMA_ALPHA) -> tuple[float, float]:
    new_mean = alpha * value + (1.0 - alpha) * mean
    dev = value - new_mean
    new_var = alpha * (dev * dev) + (1.0 - alpha) * var
    return new_mean, max(new_var, VAR_FLOOR)


def zscore(value: float, mean: float, var: float) -> float | None:
    sigma = _sigma(var)
    if sigma <= 0:
        return None
    z = (value - mean) / sigma
    zf = float(z)
    if not (zf == zf):
        return None
    return zf


def update_tick_delta(store: AdaptiveStore, symbol: str, delta_pct: float) -> None:
    st = _sym(store, symbol)
    abs_delta = abs(delta_pct)
    st.tick_mu, st.tick_var = ewma_update(st.tick_mu, st.tick_var, abs_delta)
    st.tick_n += 1


def update_change_24h(store: AdaptiveStore, symbol: str, change_24h_pct: float) -> None:
    st = _sym(store, symbol)
    abs_chg = abs(change_24h_pct)
    st.chg_mu, st.chg_var = ewma_update(st.chg_mu, st.chg_var, abs_chg)
    st.chg_n += 1


def update_from_price_pair(store: AdaptiveStore, symbol: str, *, prev_price: float, price: float) -> float | None:
    if prev_price <= 0 or price <= 0:
        return None
    delta_pct = (price / prev_price - 1.0) * 100.0
    update_tick_delta(store, symbol, delta_pct)
    return delta_pct


def ignition_passes(
    store: AdaptiveStore,
    symbol: str,
    *,
    delta_pct: float,
    static_min_pct: float = STATIC_IGNITION_MIN_PCT,
) -> tuple[bool, float | None, str]:
    abs_delta = abs(delta_pct)
    st = store.symbols.get(symbol.upper())
    if st is None or st.tick_n < MIN_TICK_SAMPLES:
        return abs_delta >= static_min_pct, None, "static"
    z = zscore(abs_delta, st.tick_mu, st.tick_var)
    if z is None:
        return abs_delta >= static_min_pct, None, "static"
    eff_floor = max(TICK_FLOOR_PCT, st.tick_mu + 0.5 * _sigma(st.tick_var))
    return abs_delta >= eff_floor and z >= Z_IGNITION, z, "adaptive"


def change_24h_tier(store: AdaptiveStore, symbol: str, change_24h_pct: float) -> tuple[str | None, float | None, str]:
    move = abs(change_24h_pct)
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        if move >= STATIC_PUMP_EXTREME_PCT:
            return "extreme", None, "static"
        if move >= STATIC_RANGE_HOT_PCT:
            return "hot", None, "static"
        return None, None, "static"
    z = zscore(move, st.chg_mu, st.chg_var)
    if z is None:
        if move >= STATIC_PUMP_EXTREME_PCT:
            return "extreme", None, "static"
        if move >= STATIC_RANGE_HOT_PCT:
            return "hot", None, "static"
        return None, None, "static"
    if z >= Z_PUMP_EXTREME:
        return "extreme", z, "adaptive"
    if z >= Z_RANGE_HOT:
        return "hot", z, "adaptive"
    return None, z, "adaptive"


def adaptive_hot_pct(store: AdaptiveStore, symbol: str) -> float:
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_RANGE_HOT_PCT
    return max(STATIC_RANGE_HOT_PCT * 0.5, st.chg_mu + Z_RANGE_HOT * _sigma(st.chg_var))


def adaptive_extreme_pct(store: AdaptiveStore, symbol: str) -> float:
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_PUMP_EXTREME_PCT
    return max(STATIC_PUMP_EXTREME_PCT * 0.5, st.chg_mu + Z_PUMP_EXTREME * _sigma(st.chg_var))

ADVISORY_PHASES = frozenset({
    "impulse_initiating", "post_dump_bounce", "distribution", "exhaustion_at_high", "dump_initiating",
})

def is_advisory_phase(lifecycle: dict[str, Any] | None) -> bool:
    return isinstance(lifecycle, dict) and str(lifecycle.get("phase") or "") in ADVISORY_PHASES

def _prescan_outlier(row: dict[str, Any] | None, direction: str) -> dict[str, Any]:
    ol = (row or {}).get("prescan_outlier") or {}
    if not isinstance(ol, dict):
        return {}
    want = "pump" if direction == "long" else "dump"
    return ol if str(ol.get("direction") or "") == want else {}

def combined_advisory_signal(row: dict[str, Any] | None, *, direction: str) -> dict[str, Any]:
    ign = _ignition_pump(row) if direction == "long" else {}
    ol = _prescan_outlier(row, direction)
    sources = [s for s, ok in (("ignition", bool(ign)), ("outlier", bool(ol))) if ok]
    return {
        "active": bool(sources),
        "ignition_pct": float(ign.get("price_delta_pct") or 0) if ign else 0.0,
        "outlier_pct": float(ol.get("change_pct") or 0) if ol else 0.0,
        "cross_venues": int(ol.get("cross_venues") or 0) if ol else 0,
        "oi_divergence": ol.get("oi_divergence") if ol else None,
        "sources": tuple(sources),
    }

LIQ_BURST_MIN_NOTIONAL_USD = 250_000.0
LIQ_BURST_MIN_EVENTS = 5
LIQ_BURST_SIDE_SKEW = 0.65

@dataclass(frozen=True, slots=True)
class LiquidationBurst:
    symbol: str
    direction: IgnitionDirection
    total_notional_usd: float
    events: int
    score: float
    window_s: int

def detect_liquidation_burst(rollups, *, symbol, events, window_seconds=300, min_notional_usd=LIQ_BURST_MIN_NOTIONAL_USD, min_events=LIQ_BURST_MIN_EVENTS, side_skew=LIQ_BURST_SIDE_SKEW):
    if not rollups:
        return None
    total = _safe_float(rollups.get("liquidation_total_notional"), 0.0) or 0.0
    score = _safe_float(rollups.get("liquidation_score"))
    if score is None or total < min_notional_usd or events < min_events:
        return None
    long_share = 1.0 - score
    if score >= side_skew:
        direction = "pump"
    elif long_share >= side_skew:
        direction = "dump"
    else:
        return None
    return LiquidationBurst(str(symbol).upper(), direction, total, int(events), round(score, 4), int(window_seconds))

def liquidation_burst_from_streams(ws_feed, symbol, *, window_seconds=300):
    if ws_feed is None:
        return None
    return detect_liquidation_burst(ws_feed.liquidation_rollups(symbol, window_seconds=window_seconds), symbol=symbol, events=ws_feed.liquidation_events(symbol, window_seconds=window_seconds), window_seconds=window_seconds)


def format_liquidation_burst_advisory(burst: LiquidationBurst) -> str:
    sym = html.escape(str(burst.symbol).replace("USDT", "-USDT"))
    arrow = "🚀" if burst.direction == "pump" else "📉"
    bias = "short fade" if burst.direction == "pump" else "long bounce"
    notional_m = float(burst.total_notional_usd) / 1e6
    return (
        f"⚡ <b>LIQ BURST</b> {arrow} <code>{sym}</code>\n"
        f"<code>${notional_m:.2f}M</code> · <code>{burst.events}</code> events · "
        f"<code>{burst.window_s}s</code> window · score <code>{burst.score:.2f}</code>\n"
        f"Bias: <b>{bias}</b> · early advisory only\n"
        f"<i>Signal-only · liquidation radar · открывай сделку вручную.</i>"
    )


IgnitionDirection = Literal["pump", "dump"]

# Defaults mirror watch.py — override via process_ticker_snapshots kwargs if needed.
DEFAULT_WINDOW_S = 300.0
DEFAULT_MIN_PCT = 2.5
DEFAULT_MIN_VOL_DELTA_USD = 250_000.0
DEFAULT_MIN_QVOL_USD = 3_000_000.0
DEFAULT_TTL_S = 7200.0

# Majors — too noisy / already on default watchlist.
_IGNITION_SKIP = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT"})


@dataclass(slots=True)
class TickerPoint:
    price: float
    quote_volume: float
    ts: float  # epoch seconds (UTC)


@dataclass(frozen=True, slots=True)
class IgnitionEvent:
    symbol: str
    direction: IgnitionDirection
    price_delta_pct: float
    vol_delta_usd: float
    quote_volume_usd: float
    window_s: float
    ignited_at: str


@dataclass(slots=True)
class ActiveIgnition:
    symbol: str
    direction: IgnitionDirection
    price_delta_pct: float
    vol_delta_usd: float
    quote_volume_usd: float
    window_s: float
    ignited_at: str
    expires_at: str
    notified: bool = False

    def to_row(self) -> dict[str, Any]:
        return {
            "active": True,
            "direction": self.direction,
            "price_delta_pct": round(self.price_delta_pct, 2),
            "vol_delta_usd": round(self.vol_delta_usd, 0),
            "quote_volume_usd": round(self.quote_volume_usd, 0),
            "window_s": round(self.window_s, 1),
            "ignited_at": self.ignited_at,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class IgnitionState:
    prev_snapshot: dict[str, TickerPoint] = field(default_factory=dict)
    active: dict[str, ActiveIgnition] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> IgnitionState:
        prev: dict[str, TickerPoint] = {}
        for sym, pt in (raw.get("prev_snapshot") or {}).items():
            if not isinstance(pt, dict):
                continue
            price = _safe_float(pt.get("price"))
            vol = _safe_float(pt.get("quote_volume"))
            ts = _safe_float(pt.get("ts"))
            if price and price > 0 and vol is not None and ts is not None:
                prev[str(sym).upper()] = TickerPoint(price=price, quote_volume=vol, ts=ts)
        active: dict[str, ActiveIgnition] = {}
        for sym, item in (raw.get("active") or {}).items():
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction") or "pump")
            if direction not in ("pump", "dump"):
                direction = "pump"
            active[str(sym).upper()] = ActiveIgnition(
                symbol=str(sym).upper(),
                direction=direction,  # type: ignore[arg-type]
                price_delta_pct=float(item.get("price_delta_pct") or 0),
                vol_delta_usd=float(item.get("vol_delta_usd") or 0),
                quote_volume_usd=float(item.get("quote_volume_usd") or 0),
                window_s=float(item.get("window_s") or 0),
                ignited_at=str(item.get("ignited_at") or ""),
                expires_at=str(item.get("expires_at") or ""),
                notified=bool(item.get("notified")),
            )
        return cls(prev_snapshot=prev, active=active)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prev_snapshot": {
                sym: {
                    "price": pt.price,
                    "quote_volume": pt.quote_volume,
                    "ts": pt.ts,
                }
                for sym, pt in self.prev_snapshot.items()
            },
            "active": {
                sym: {
                    **asdict(ig),
                }
                for sym, ig in self.active.items()
            },
        }


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def detect_ignitions(
    current_rows: list[dict[str, Any]],
    prev: dict[str, TickerPoint],
    *,
    now: datetime,
    now_ts: float,
    window_s: float = DEFAULT_WINDOW_S,
    min_pct: float = DEFAULT_MIN_PCT,
    min_vol_delta_usd: float = DEFAULT_MIN_VOL_DELTA_USD,
    min_qvol_usd: float = DEFAULT_MIN_QVOL_USD,
    adaptive: AdaptiveStore | None = None,
) -> tuple[list[IgnitionEvent], dict[str, TickerPoint]]:
    """Compare ticker rows to previous snapshot; return new ignition events + updated snapshot."""
    events: list[IgnitionEvent] = []
    next_snap: dict[str, TickerPoint] = {}

    for row in current_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or sym in _IGNITION_SKIP:
            continue
        price = _safe_float(row.get("last_price"))
        quote_volume = _safe_float(row.get("quote_volume"), 0.0)
        if price is None or price <= 0 or quote_volume is None or quote_volume <= 0:
            continue
        next_snap[sym] = TickerPoint(price=price, quote_volume=quote_volume, ts=now_ts)

        baseline = prev.get(sym)
        if baseline is None:
            continue
        age_s = now_ts - baseline.ts
        if age_s <= 0 or age_s > window_s:
            continue
        if quote_volume < min_qvol_usd:
            continue
        price_delta_pct = (price / baseline.price - 1.0) * 100.0
        vol_delta = quote_volume - baseline.quote_volume
        if adaptive is not None:
            move_ok, _z, _mode = ignition_passes(
                adaptive, sym, delta_pct=price_delta_pct, static_min_pct=min_pct
            )
            update_tick_delta(adaptive, sym, price_delta_pct)
        else:
            move_ok = abs(price_delta_pct) >= min_pct
        if not move_ok or vol_delta < min_vol_delta_usd:
            continue
        direction: IgnitionDirection = "pump" if price_delta_pct > 0 else "dump"
        events.append(
            IgnitionEvent(
                symbol=sym,
                direction=direction,
                price_delta_pct=price_delta_pct,
                vol_delta_usd=vol_delta,
                quote_volume_usd=quote_volume,
                window_s=age_s,
                ignited_at=now.isoformat(),
            )
        )

    return events, next_snap


def merge_ignitions(
    state: IgnitionState,
    events: list[IgnitionEvent],
    *,
    now: datetime,
    ttl_s: float = DEFAULT_TTL_S,
) -> tuple[list[IgnitionEvent], IgnitionState]:
    """Apply TTL expiry, register new ignitions; return only newly-added events."""
    expires_cutoff = now
    active: dict[str, ActiveIgnition] = {}
    for sym, ig in state.active.items():
        try:
            exp = datetime.fromisoformat(ig.expires_at)
        except ValueError:
            continue
        if exp > expires_cutoff:
            active[sym] = ig

    new_events: list[IgnitionEvent] = []
    for ev in events:
        sym = ev.symbol.upper()
        if sym in active:
            # Refresh TTL on repeat spike within window.
            active[sym] = ActiveIgnition(
                symbol=sym,
                direction=ev.direction,
                price_delta_pct=ev.price_delta_pct,
                vol_delta_usd=ev.vol_delta_usd,
                quote_volume_usd=ev.quote_volume_usd,
                window_s=ev.window_s,
                ignited_at=ev.ignited_at,
                expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
                notified=active[sym].notified,
            )
            continue
        active[sym] = ActiveIgnition(
            symbol=sym,
            direction=ev.direction,
            price_delta_pct=ev.price_delta_pct,
            vol_delta_usd=ev.vol_delta_usd,
            quote_volume_usd=ev.quote_volume_usd,
            window_s=ev.window_s,
            ignited_at=ev.ignited_at,
            expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
            notified=False,
        )
        new_events.append(ev)

    state.active = active
    return new_events, state


def process_ticker_snapshots(
    ticker_rows: list[dict[str, Any]],
    state: IgnitionState,
    *,
    now: datetime | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    min_pct: float = DEFAULT_MIN_PCT,
    min_vol_delta_usd: float = DEFAULT_MIN_VOL_DELTA_USD,
    min_qvol_usd: float = DEFAULT_MIN_QVOL_USD,
    ttl_s: float = DEFAULT_TTL_S,
    adaptive: AdaptiveStore | None = None,
) -> tuple[list[IgnitionEvent], IgnitionState]:
    """Full tick: detect → merge → refresh prev snapshot."""
    now = now or datetime.now(UTC)
    now_ts = now.timestamp()
    events, next_snap = detect_ignitions(
        ticker_rows,
        state.prev_snapshot,
        now=now,
        now_ts=now_ts,
        window_s=window_s,
        min_pct=min_pct,
        min_vol_delta_usd=min_vol_delta_usd,
        min_qvol_usd=min_qvol_usd,
        adaptive=adaptive,
    )
    state.prev_snapshot = next_snap
    new_events, state = merge_ignitions(state, events, now=now, ttl_s=ttl_s)
    return new_events, state


def load_ignition_state(path: Path = IGNITION_STATE) -> IgnitionState:
    if not path.exists():
        return IgnitionState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return IgnitionState()
    if not isinstance(raw, dict):
        return IgnitionState()
    return IgnitionState.from_dict(raw)


def save_ignition_state(state: IgnitionState, path: Path = IGNITION_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def active_ignition_map(state: IgnitionState) -> dict[str, dict[str, Any]]:
    return {sym: ig.to_row() for sym, ig in state.active.items()}


def mark_ignition_notified(state: IgnitionState, symbol: str) -> None:
    sym = symbol.upper()
    if sym in state.active:
        state.active[sym].notified = True


def pending_ignition_alerts(state: IgnitionState) -> list[ActiveIgnition]:
    return [ig for ig in state.active.values() if not ig.notified]


def format_ignition_telegram(ig: ActiveIgnition | IgnitionEvent) -> str:
    sym = str(ig.symbol).replace("USDT", "-USDT")
    direction = ig.direction
    arrow = "🚀" if direction == "pump" else "📉"
    bias = "short fade" if direction == "pump" else "long bounce"
    pct = ig.price_delta_pct
    vol_m = ig.vol_delta_usd / 1e6
    qvol_m = ig.quote_volume_usd / 1e6
    window = getattr(ig, "window_s", 0)
    return (
        f"🔥 <b>IGNITION</b> {arrow} <code>{sym}</code>\n"
        f"<code>{pct:+.2f}%</code> in <code>{window:.0f}s</code> · "
        f"vol +<code>${vol_m:.2f}M</code> · 24h qvol <code>${qvol_m:.1f}M</code>\n"
        f"Watch bias: <b>{bias}</b> · added to minute-watch\n"
        f"<i>Signal-only · ignition radar · открывай сделку вручную.</i>"
    )

EarlyKind = Literal["none", "prep", "imminent", "start", "confirm"]

SHORT_PREP_LC = frozenset({"exhaustion_at_high", "distribution"})
LONG_PREP_LC = frozenset({
    "post_dump_bounce",
    "accumulation",
    "recovery",
    "breakout_arming",
    "impulse_initiating",
})

SHORT_PREP_SETUP = frozenset({"exhaustion_watch", "dump_setup_forming"})
SHORT_START_SETUP = frozenset({"dump_imminent", "dump_initiating"})
LONG_PREP_SETUP = frozenset({"accumulation_watch", "long_setup_forming"})
LONG_START_SETUP = frozenset({"long_imminent", "long_initiating"})

EARLY_COOLDOWN_MIN = {
    "prep": 12,
    "imminent": 8,
    "start": 6,
}


def early_telegram_enabled(symbol: str) -> bool:
    if EARLY_TELEGRAM_ENABLED:
        return True
    flags = watchlist_flags(symbol)
    return bool(flags.get("early_telegram") or flags.get("dump_hunt"))

# Prep/start spam without tracker outcomes — per-symbol dump_hunt enables early TG.
EARLY_TELEGRAM_ENABLED = os.getenv("HUNT_EARLY_DUMP_TG", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
TP1_PARTIAL_FIX_PCT = 80


@dataclass(frozen=True, slots=True)
class EarlyAlert:
    kind: EarlyKind
    tier: str  # prep | imminent | start
    message: str


def _lc(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    phase = getattr(lifecycle, "phase", None)
    if hasattr(phase, "value"):
        phase = phase.value
    return {
        "phase": phase,
        "recommended_bias": getattr(lifecycle, "recommended_bias", None),
        "short_entry_ok": getattr(lifecycle, "short_entry_ok", None),
        "fall_from_high_pct": getattr(lifecycle, "fall_from_high_pct", None),
        "bounce_from_low_pct": getattr(lifecycle, "bounce_from_low_pct", None),
    }


def _fuel(setup: dict[str, Any], direction: str) -> float:
    if direction == "short":
        return max(
            float(setup.get("dump_fuel") or 0),
            float(setup.get("dump_score") or 0),
        )
    return max(
        float(setup.get("long_fuel") or 0),
        float(setup.get("long_score") or 0),
    )


def _ignition_pump(row: dict[str, Any] | None) -> dict[str, Any]:
    ign = (row or {}).get("ignition") or {}
    if str(ign.get("direction") or "") == "pump" and ign.get("active"):
        return ign
    return {}


def evaluate_early_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> EarlyAlert:
    """Whether to send preparation/start Telegram (separate from full confirm)."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    wl = watchlist_flags(sym)
    dump_hunt = bool(wl.get("dump_hunt"))
    lc = _lc(lifecycle)
    lc_phase = str(lc.get("phase") or "")
    setup_phase = str(setup.get("phase") or "")
    fuel = _fuel(setup, direction)
    confirmed = bool(setup.get("confirmed"))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    triggers = [str(t) for t in (setup.get("triggers") or [])]

    if direction == "short":
        price = float((row or {}).get("price") or 0)
        from hunt_core.gate.delivery import price_in_entry_zone  # noqa: PLC0415

        in_zone = price > 0 and price_in_entry_zone(setup, price, direction="short")
        if lc_phase not in SHORT_PREP_LC and not (
            lc_phase == "dump_active" and setup_phase in SHORT_START_SETUP
        ):
            return EarlyAlert("none", "", "")
        if lc_phase in SHORT_PREP_LC and not lc.get("short_entry_ok", True):
            if not (in_zone and setup_phase in SHORT_START_SETUP | {"dump_setup_forming"}):
                return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        fall_pct = float(lc.get("fall_from_high_pct") or 0)
        if (
            in_zone
            and setup_phase in SHORT_START_SETUP | {"dump_setup_forming", "dump_imminent"}
            and fuel >= cal.forming_min_score
            and fall_pct < 3.0
        ):
            return EarlyAlert(
                "start",
                "start",
                f"В зоне входа · {setup_phase} · fuel {fuel:.0f} · жди/входи по confirm",
            )

        support = float(setup.get("support_break_level") or 0)
        below_support = support > 0 and price > 0 and price < support

        if dump_hunt and below_support and fuel >= cal.forming_min_score + 10:
            return EarlyAlert(
                "start",
                "start",
                f"Пробой support {support:.5f} · fuel {fuel:.0f} · открывай шорт",
            )

        if dump_hunt and fuel >= cal.confirm_min_score and setup_phase in SHORT_PREP_SETUP | SHORT_START_SETUP:
            if below_support or any(
                k in h
                for h in hard
                for k in ("close_below_support", "live_below_support", "rejection", "cascade")
            ):
                return EarlyAlert(
                    "imminent",
                    "imminent",
                    f"Dump hunt armed · {setup_phase} · fuel {fuel:.0f}",
                )

        if setup_phase in SHORT_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in (
                    "close_below_support",
                    "live_below_support",
                    "rejection",
                    "cascade",
                    "lost_support",
                )
            )
            if has_struct or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Дамп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if setup_phase == "dump_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Дамп imminent · fuel {fuel:.0f} · жди closed-bar",
            )

        if (
            dump_hunt
            and lc_phase in SHORT_PREP_LC
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming"}
            and fuel >= cal.forming_min_score + 20
        ):
            fall = float(lc.get("fall_from_high_pct") or 0)
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Fade-zone charged · fuel {fuel:.0f} · fall {fall:.1f}%",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            fall = float(lc.get("fall_from_high_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка шорта · {lc_phase} · fuel {fuel:.0f} · fall {fall:.1f}%",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and fuel >= cal.confirm_min_score - 5
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming", "exhaustion_watch"}
        ):
            return EarlyAlert(
                "prep",
                "prep",
                f"Fade-zone watch · fuel {fuel:.0f} · {setup_phase}",
            )

    else:
        ign = _ignition_pump(row)
        ign_pct = float(ign.get("price_delta_pct") or 0)
        long_ok_phase = lc_phase in LONG_PREP_LC or bool(ign)

        if not long_ok_phase:
            return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        broke_res = any("broke_resistance" in t for t in triggers)
        if setup_phase in LONG_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in ("close_above_resistance", "bounce", "cascade", "broke_resistance")
            )
            if has_struct or broke_res or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Памп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if ign and ign_pct >= 2.0 and fuel >= cal.forming_min_score:
            if broke_res or setup_phase in LONG_START_SETUP:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Ignition +{ign_pct:.1f}% · {setup_phase} · fuel {fuel:.0f}",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Ignition заряд +{ign_pct:.1f}% · {lc_phase or 'pump'} · fuel {fuel:.0f}",
            )

        if setup_phase == "long_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Памп imminent · fuel {fuel:.0f}",
            )

        if lc_phase == "impulse_initiating" and fuel >= cal.forming_min_score:
            rally = float(lc.get("bounce_from_low_pct") or 0)
            if broke_res or fuel >= cal.confirm_min_score - 8:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Импульс вверх · fuel {fuel:.0f} · rally {rally:.1f}%",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Импульс формируется · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

        if lc_phase == "breakout_arming" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "prep",
                "prep",
                f"База заряжена (squeeze) · fuel {fuel:.0f} · жди пробой",
            )

        if (
            setup_phase in LONG_PREP_SETUP | {"long_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            rally = float(lc.get("bounce_from_low_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка лонга · {lc_phase} · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

    return EarlyAlert("none", "", "")


def early_cooldown_ok(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> bool:
    if tier not in EARLY_COOLDOWN_MIN:
        return True
    # Tier hierarchy: after 🚨 start was sent, re-sending 🟡 prep for the same
    # symbol+direction inside the window is noise (prep↔start oscillation gave
    # 76 would-sends on a 2-symbol replay) — an equal-or-higher tier on cooldown
    # silences this one too.
    order = ("prep", "imminent", "start")
    rank = order.index(tier) if tier in order else 0
    for other in order[rank:]:
        key = f"early:{symbol.upper()}:{direction.lower()}:{other}"
        raw = state.get(key)
        if not raw:
            continue
        try:
            last = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if now - last < timedelta(minutes=EARLY_COOLDOWN_MIN.get(other, 30)):
            return False
    return True


def mark_early_sent(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> None:
    state[f"early:{symbol.upper()}:{direction.lower()}:{tier}"] = now.isoformat()


def format_early_telegram(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: Any | None,
    alert: EarlyAlert,
) -> str:
    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    lc = _lc(lifecycle)
    fuel = _fuel(setup, direction)
    price = row.get("price")
    chg = row.get("chg_24h_pct")
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    setup_phase = html.escape(str(setup.get("phase") or "—"))
    ign = _ignition_pump(row)
    ign_txt = (
        f" · ignition <code>+{float(ign.get('price_delta_pct') or 0):.1f}%</code>"
        if ign
        else ""
    )

    if direction == "short":
        badge = {"prep": "🟠", "imminent": "🔴", "start": "🚨"}.get(alert.tier, "🔴")
        title = {"prep": "DUMP PREP", "imminent": "DUMP IMMINENT", "start": "DUMP START"}.get(
            alert.tier, "DUMP WATCH"
        )
    else:
        badge = {"prep": "🟡", "imminent": "🟢", "start": "🚨"}.get(alert.tier, "🟢")
        title = {"prep": "PUMP PREP", "imminent": "PUMP IMMINENT", "start": "PUMP START"}.get(
            alert.tier, "PUMP WATCH"
        )

    hard = setup.get("confirm_hard") or []
    triggers = setup.get("triggers") or []
    hard_txt = html.escape(", ".join(str(h) for h in hard[:5]))
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))

    lines = [
        f"{badge} <b>{title}</b> {sym}",
        f"<i>{html.escape(alert.message)}</i>",
        f"Цена <code>{price}</code> · 24h <code>{chg}%</code>{ign_txt}",
        f"Lifecycle <code>{lc_phase}</code> · setup <code>{setup_phase}</code> · fuel <code>{fuel:.0f}</code>",
    ]
    if hard_txt:
        lines.append(f"Hard partial: <code>{hard_txt}</code>")
    if trig_txt:
        lines.append(f"Triggers: <code>{trig_txt}</code>")
    ez = setup.get("entry_zone") or []
    if len(ez) >= 2:
        lines.append(
            f"Entry zone <code>{ez[0]}</code>–<code>{ez[1]}</code> · "
            f"SL <code>{setup.get('stop_loss')}</code> · TP1 <code>{setup.get('tp1')}</code>"
        )
    lines.append("<i>Early hunt alert · prep/start — не auto-trade</i>")
    return "\n".join(lines)

__all__ = [
    "DeliveryMode",
    "DetectorPath",
    "SetupCandidate",
    "chart_pattern_trigger",
    "cluster_fuel",
    "compute_setup_fuel",
    "confirm_dump",
    "confirm_long",
    "ema200_confluence_trigger",
    "hidden_div_trigger",
    "candle_pattern_fuel_triggers",
    "candle_pattern_hard_triggers",
    "polars_ta_fuel_triggers",
    "research_fuel_triggers",
    "distribution_fuel_triggers",
    "enrich_dump_setup",
    "enrich_long_setup",
    "long_resistance_chase_veto",
    "phase_dump",
    "phase_long",
    "resolve_delivery_mode",
    "route_tick",
    "squeeze_at_boundary_trigger",
    "wall_depth_fuel_triggers",
    "ws_cvd_divergence_fuel_triggers",
    "ws_depth_fuel_triggers",
    "detect_pp",
    "score_dump_init",
    "AdaptiveStore",
    "SymbolAdaptive",
    "load_adaptive_store",
    "save_adaptive_store",
    "adaptive_extreme_pct",
    "adaptive_hot_pct",
    "change_24h_tier",
    "ewma_update",
    "update_change_24h",
    "update_from_price_pair",
    "update_tick_delta",
    "zscore",
    "ADVISORY_PHASES",
    "ActiveIgnition",
    "EarlyAlert",
    "EarlyKind",
    "IgnitionDirection",
    "IgnitionEvent",
    "IgnitionState",
    "LiquidationBurst",
    "TickerPoint",
    "active_ignition_map",
    "combined_advisory_signal",
    "detect_ignitions",
    "detect_liquidation_burst",
    "early_cooldown_ok",
    "early_telegram_enabled",
    "evaluate_early_alert",
    "format_early_telegram",
    "format_ignition_telegram",
    "format_liquidation_burst_advisory",
    "ignition_passes",
    "is_advisory_phase",
    "liquidation_burst_from_streams",
    "load_ignition_state",
    "mark_early_sent",
    "mark_ignition_notified",
    "merge_ignitions",
    "pending_ignition_alerts",
    "process_ticker_snapshots",
    "save_ignition_state",
]
