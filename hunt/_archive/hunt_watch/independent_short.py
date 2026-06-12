"""Independent BEAT short confirmation — raw indicators + REST, no hunt gates."""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_watch.paths import DATA

FADE_ZONE_FALLBACK = (8.15, 8.37)
COOLDOWN_MIN = 45
STATE_PATH = DATA / "beat_short_watch_state.json"


@dataclass(slots=True)
class ShortVerdict:
    confirmed: bool
    setup: str  # none | fade | momentum | dump_continuation
    reasons: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    levels: dict[str, Any] = field(default_factory=dict)


def _feat(matrix: dict[str, Any], tf: str, key: str, default: float | None = None) -> float | None:
    panel = matrix.get(tf) or {}
    v = panel.get(key)
    if v is None:
        return default
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _fade_zone(row: dict[str, Any], don_hi: float) -> tuple[float, float]:
    lc = row.get("lifecycle") or {}
    lv = row.get("levels") or {}
    res = float(lc.get("local_resistance") or lv.get("invalidation_above") or don_hi or 0)
    if res <= 0:
        return FADE_ZONE_FALLBACK
    return round(res * 0.96, 4), round(res * 1.02, 4)


def _geom_levels(
    price: float,
    *,
    donchian_high: float,
    atr15: float,
    fade_zone: tuple[float, float],
) -> dict[str, Any]:
    entry_hi = max(donchian_high * 0.998, fade_zone[1])
    entry_lo = fade_zone[0]
    stop = round(max(donchian_high * 1.05, price * 1.09, entry_hi + atr15 * 1.2), 4)
    tp1 = round(price - atr15 * 3.5, 4)
    tp2 = round(price - atr15 * 6.0, 4)
    risk = stop - price if price > 0 else 0
    reward = price - tp1 if price > 0 else 0
    rr = round(reward / risk, 2) if risk > 0 else None
    return {
        "entry_zone": [round(entry_lo, 4), round(entry_hi, 4)],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk_reward": rr,
        "donchian_high": donchian_high,
    }


def evaluate_independent_short(
    row: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
) -> ShortVerdict:
    """Confirm short from beat_dump tick row (feature_matrix + rest_pack_summary)."""
    if row.get("analyzable") is False or row.get("verdict") == "DATA_INCOMPLETE":
        return ShortVerdict(False, "none", blocks=[str(row.get("error") or "data_incomplete")])

    price = float(row.get("price") or 0)
    matrix = row.get("feature_matrix") or {}
    rest = row.get("rest_pack_summary") or {}
    lc = row.get("lifecycle") or {}
    prior = prior or {}

    f5 = "5m_closed"
    f15 = "15m_closed"
    f1h = "1h_closed"
    f1 = "1m_closed"

    rsi5 = _feat(matrix, f5, "rsi14")
    rsi15 = _feat(matrix, f15, "rsi14")
    rsi1h = _feat(matrix, f1h, "rsi14")
    rsi1 = _feat(matrix, f1, "rsi14")
    ema20_5 = _feat(matrix, f5, "ema20")
    st15 = _feat(matrix, f15, "supertrend_dir")
    macd5 = _feat(matrix, f5, "macd_hist")
    vwap15 = _feat(matrix, f15, "vwap_deviation_atr14")
    don_hi = _feat(matrix, f5, "donchian_high20", price)

    taker15 = float(rest.get("taker_15m") or 0)
    taker5 = float(rest.get("taker_5m") or 0) if rest.get("taker_5m") else None
    oi_chg_1h = float(rest.get("oi_chg_1h") or 0)
    gls_z = float(rest.get("gls_z") or 0)

    prev_rsi5 = prior.get("rsi5_closed")
    prev_rsi15 = prior.get("rsi15_closed")

    atr15 = _feat(matrix, f15, "atr14", 0.35) or 0.35
    fade_zone = _fade_zone(row, float(don_hi or FADE_ZONE_FALLBACK[1]))
    levels = _geom_levels(
        price,
        donchian_high=float(don_hi or fade_zone[1]),
        atr15=atr15,
        fade_zone=fade_zone,
    )

    reasons: list[str] = []
    blocks: list[str] = []

    phase = str(lc.get("phase") or "")
    fall = float(lc.get("fall_from_high_pct") or 0)
    local_sup = float(
        lc.get("local_support")
        or (row.get("levels") or {}).get("local_support")
        or 0
    )
    composite = float(row.get("composite_dump_score") or 0)
    cluster_tb = float((row.get("cluster_scores") or {}).get("trend_break") or 0)

    in_fade_zone = fade_zone[0] <= price <= fade_zone[1] or price >= float(don_hi or 0) * 0.97
    htf_hot = (rsi15 or 0) >= 68 or (rsi1h or 0) >= 68

    rejection_signals = 0
    if st15 is not None and st15 < 0:
        rejection_signals += 1
        reasons.append("15m_supertrend_bear")
    if macd5 is not None and macd5 < 0:
        rejection_signals += 1
        reasons.append("5m_macd_hist_negative")
    if prev_rsi5 is not None and rsi5 is not None and rsi5 < prev_rsi5 - 1.5:
        rejection_signals += 1
        reasons.append(f"5m_rsi_rollover {prev_rsi5:.1f}->{rsi5:.1f}")
    if rsi1 is not None and rsi1 < 60:
        rejection_signals += 1
        reasons.append(f"1m_rsi_cooling_{rsi1:.0f}")

    flow_sell = taker15 < 1.12 and (taker5 is None or taker5 < 1.08)
    crowd_unwind = oi_chg_1h <= 0 or gls_z >= 1.0

    # Path A: fade at resistance after parabolic extension
    fade_ok = (
        in_fade_zone
        and htf_hot
        and rejection_signals >= 2
        and flow_sell
        and crowd_unwind
        and (vwap15 or 0) >= 3.5
    )

    # Path B: momentum rollover (no rip required)
    momentum_ok = False
    if ema20_5 and price < ema20_5 and taker15 < 1.0 and oi_chg_1h < -0.003:
        if prev_rsi15 is not None and (rsi15 or 0) < 76 and prev_rsi15 >= 76:
            momentum_ok = True
            reasons.append(f"momentum_rollover 15m_rsi {prev_rsi15:.1f}->{rsi15:.1f}")
            reasons.append(f"below_5m_ema20_{ema20_5:.4f}")
            reasons.append(f"oi_flush_1h_{oi_chg_1h*100:.2f}%")

    # Path C: mid-dump continuation (BEAT 8.37→6.7 — fade zone irrelevant)
    dump_structural = 0
    if local_sup > 0 and price < local_sup * 0.998:
        dump_structural += 1
        reasons.append(f"below_local_support_{local_sup:.4f}")
    if st15 is not None and st15 < 0:
        dump_structural += 1
    if macd5 is not None and macd5 < 0:
        dump_structural += 1
    if rejection_signals >= 1:
        dump_structural += 1
    fuel_ok = composite >= 0.52 or cluster_tb >= 0.38
    bear_trend = (st15 is not None and st15 < 0) or (macd5 is not None and macd5 < 0)
    # fall≥15%: continuation short even if lifecycle says post_dump_bounce (BEAT 8→6.7).
    dump_ok = (
        fall >= 15.0
        and phase not in {"accumulation", "recovery"}
        and dump_structural >= 2
        and fuel_ok
        and bear_trend
    )
    if dump_ok:
        reasons.append(f"dump_continuation_fall{fall:.1f}%_phase={phase}")

    if fade_ok:
        return ShortVerdict(True, "fade", reasons=reasons, levels=levels)
    if momentum_ok:
        return ShortVerdict(True, "momentum", reasons=reasons, levels=levels)
    if dump_ok:
        return ShortVerdict(True, "dump_continuation", reasons=reasons, levels=levels)

    if not in_fade_zone and fall < 12.0:
        blocks.append(f"price {price:.4f} outside fade {fade_zone[0]}-{fade_zone[1]}")
    if not htf_hot and not dump_ok:
        blocks.append(f"HTF RSI not extended (15m={rsi15}, 1h={rsi1h})")
    if rejection_signals < 2 and not dump_ok:
        blocks.append(f"rejection_signals={rejection_signals}/2")
    if not flow_sell and not dump_ok:
        blocks.append(f"taker still buy 15m={taker15:.3f}")
    if not crowd_unwind and not dump_ok:
        blocks.append(f"no unwind oi_1h={oi_chg_1h:.4f} gls_z={gls_z:.2f}")
    if phase in {"dump_active", "distribution"} and fall >= 12.0 and not dump_ok:
        blocks.append(
            f"dump_not_ready structural={dump_structural}/2 fuel={composite:.2f}"
        )

    return ShortVerdict(False, "none", reasons=reasons, blocks=blocks, levels=levels)


def load_watch_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_watch_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cooldown_open(symbol: str, state: dict[str, Any], *, minutes: int = COOLDOWN_MIN) -> bool:
    key = f"last_sent:{symbol.upper()}"
    raw = state.get(key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return datetime.now(UTC) - last >= timedelta(minutes=minutes)


def mark_sent(symbol: str, state: dict[str, Any]) -> None:
    state[f"last_sent:{symbol.upper()}"] = datetime.now(UTC).isoformat()


def format_telegram_short(
    symbol: str,
    row: dict[str, Any],
    verdict: ShortVerdict,
) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    lv = verdict.levels
    ez = lv.get("entry_zone") or []
    lc = row.get("lifecycle") or {}
    lines = [
        f"🔴 <b>SHORT CONFIRMED</b> {sym}",
        f"<b>Setup:</b> <code>{html.escape(verdict.setup)}</code>",
        f"Цена <code>{row.get('price')}</code> · 24h <code>{row.get('chg_24h_pct')}%</code>",
        f"Fall <code>{lc.get('fall_from_high_pct')}%</code> · lc <code>{lc.get('phase')}</code>",
        "",
        "<b>Причины:</b>",
    ]
    for r in verdict.reasons[:10]:
        lines.append(f"• <code>{html.escape(r)}</code>")
    if ez:
        lines.append(
            f"\n<b>Уровни:</b> entry <code>{ez[0]}-{ez[1]}</code>"
        )
        lines.append(
            f"SL <code>{lv.get('stop_loss')}</code> · TP1 <code>{lv.get('tp1')}</code> · "
            f"TP2 <code>{lv.get('tp2')}</code> · R:R <code>{lv.get('risk_reward')}</code>"
        )
    rest = row.get("rest_pack_summary") or {}
    lines.append(
        f"\n<i>REST: OI z {rest.get('oi_z')} · taker15m {rest.get('taker_15m')} · "
        f"funding {float(rest.get('funding') or 0)*100:.3f}%</i>"
    )
    return "\n".join(lines)


def status_line(row: dict[str, Any], verdict: ShortVerdict) -> str:
    blocks = "; ".join(verdict.blocks[:4]) if verdict.blocks else "—"
    return (
        f"price={row.get('price')} confirmed={verdict.confirmed} setup={verdict.setup} "
        f"blocks={blocks}"
    )
