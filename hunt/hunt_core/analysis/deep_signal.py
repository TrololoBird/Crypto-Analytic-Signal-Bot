"""Deep analysis — BTC alignment, MTF, liquidity, order flow, POC scenarios."""
from __future__ import annotations


from dataclasses import dataclass, field
from typing import Any, Literal


# Tiered BTC corr (live probe 2026-06-10: meme alts 0.1–0.24, SOXL 0.67).
BTC_CORR_SOFT = 0.45
BTC_CORR_HARD = 0.70
BTC_CORR_SIGNIFICANT = BTC_CORR_HARD  # legacy alias
BTC_TREND_MIN_CHG_PCT = 0.12


def btc_market_context(btc_work_1h: Any | None) -> dict[str, Any]:
    """1h/4h BTC change and trend label from prepared 1h frame."""
    if btc_work_1h is None or getattr(btc_work_1h, "is_empty", lambda: True)():
        return {}
    try:
        closes = [float(x) for x in btc_work_1h["close"].to_list()]
    except (TypeError, KeyError, ValueError):
        return {}
    if len(closes) < 3:
        return {}
    chg_1h = (closes[-1] / closes[-2] - 1.0) * 100.0
    chg_4h = (closes[-1] / closes[-5] - 1.0) * 100.0 if len(closes) >= 5 else None
    if chg_1h >= BTC_TREND_MIN_CHG_PCT:
        trend = "up"
    elif chg_1h <= -BTC_TREND_MIN_CHG_PCT:
        trend = "down"
    else:
        trend = "flat"
    return {
        "btc_chg_1h_pct": round(chg_1h, 2),
        "btc_chg_4h_pct": round(chg_4h, 2) if chg_4h is not None else None,
        "btc_trend": trend,
    }


_SHORT_BIAS_PHASES = frozenset(
    {"exhaustion_at_high", "distribution", "dump_active"}
)
_LONG_BIAS_PHASES = frozenset(
    {
        "post_dump_bounce",
        "accumulation",
        "recovery",
        "breakout_arming",
        "impulse_initiating",
    }
)
# Confirm gaps must reference actionable levels near price — not ancient impulse highs.
_CONFIRM_LEVEL_MAX_DIST_PCT = 12.0


def _fmt_confirm_price(val: float) -> str:
    if val >= 1:
        return f"{val:.4f}".rstrip("0").rstrip(".")
    if val >= 0.01:
        return f"{val:.6f}".rstrip("0").rstrip(".")
    return f"{val:.8f}".rstrip("0").rstrip(".")


def _level_within_pct(level: float, price: float, *, max_pct: float) -> bool:
    if level <= 0 or price <= 0:
        return False
    return abs(level - price) / price * 100.0 <= max_pct


def local_confirm_level(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None = None,
    price: float = 0.0,
    max_dist_pct: float = _CONFIRM_LEVEL_MAX_DIST_PCT,
) -> float:
    """Nearest closed-bar confirm level — capped distance from spot."""
    px = price or float((row or {}).get("price") or 0)
    lc = (row or {}).get("lifecycle") or {}
    if direction == "long":
        ez = setup.get("entry_zone") or [px, px]
        entry_hi = float(ez[1] if len(ez) > 1 else px or 0)
        for raw in (
            setup.get("local_resistance"),
            lc.get("local_resistance"),
            setup.get("tp1"),
            setup.get("resistance_break_level"),
        ):
            lvl = float(raw or 0)
            if lvl > px and _level_within_pct(lvl, px, max_pct=max_dist_pct):
                return lvl
        if px > 0:
            return round(max(entry_hi * 1.005, px * 1.008), 8)
        return float(setup.get("resistance_break_level") or 0)
    support = float(setup.get("support_break_level") or 0)
    for raw in (
        setup.get("local_support"),
        lc.get("local_support"),
        setup.get("tp1"),
        support,
    ):
        lvl = float(raw or 0)
        if lvl > 0 and lvl < px and _level_within_pct(lvl, px, max_pct=max_dist_pct):
            return lvl
    ez = setup.get("entry_zone") or [px, px]
    entry_lo = float(ez[0] if ez else px or 0)
    if px > 0:
        return round(min(entry_lo * 0.995, px * 0.992), 8) if entry_lo > 0 else round(px * 0.992, 8)
    return support


def local_confirm_label(
    setup: dict[str, Any],
    *,
    direction: str,
    level: float,
    row: dict[str, Any] | None = None,
) -> str:
    lc = (row or {}).get("lifecycle") or {}
    if direction == "long":
        tp1 = float(setup.get("tp1") or 0)
        if tp1 > 0 and abs(level - tp1) / tp1 <= 0.025:
            return "локальный TP1/POC"
        lr = float(setup.get("local_resistance") or lc.get("local_resistance") or 0)
        if lr > 0 and abs(level - lr) / lr <= 0.025:
            return "локальный pivot"
        return "локальный resistance"
    tp1 = float(setup.get("tp1") or 0)
    if tp1 > 0 and abs(level - tp1) / tp1 <= 0.025:
        return "локальный TP1"
    return "локальный support"


def resolve_trade_direction(
    row: dict[str, Any],
) -> tuple[str, dict[str, Any], float, list[str]]:
    """Lifecycle bias first, then BTC corr, then fuel (BEAT long-fuel vs short-bias fix)."""
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}
    short_fuel = float(dump.get("dump_fuel") or 0)
    long_fuel = float(long_setup.get("long_fuel") or 0)
    bias = str(lc.get("recommended_bias") or "")
    phase = str(lc.get("phase") or "")
    notes: list[str] = []

    if bias == "short" and phase in _SHORT_BIAS_PHASES:
        if short_fuel >= 40 or short_fuel >= long_fuel - 12:
            direction = "short"
            notes.append(f"lifecycle bias=short phase={phase}")
        elif long_fuel >= 75 and short_fuel < 45:
            direction = "long"
            notes.append("long fuel override при weak short")
        else:
            direction = "short"
            notes.append("bias short — приоритет SHORT даже при lower fuel")
    elif bias == "long" and phase in _LONG_BIAS_PHASES:
        if long_fuel >= 40 or long_fuel >= short_fuel - 12:
            direction = "long"
            notes.append(f"lifecycle bias=long phase={phase}")
        elif short_fuel >= 75 and long_fuel < 45:
            direction = "short"
            notes.append("short fuel override при weak long")
        else:
            direction = "long"
            notes.append("bias long — приоритет LONG")
    elif bias == "wait":
        direction = "short" if short_fuel >= long_fuel else "long"
        notes.append("bias=wait — monitor only, pick higher fuel")
    else:
        corr_raw = (row.get("regime") or {}).get("btc_corr_1h")
        direction, notes = correlated_direction(
            short_fuel=short_fuel,
            long_fuel=long_fuel,
            btc_corr_1h=float(corr_raw) if corr_raw is not None else None,
            btc_trend=str((row.get("btc_context") or {}).get("btc_trend") or "flat"),
            symbol=str(row.get("symbol") or ""),
        )

    setup = dump if direction == "short" else long_setup
    fuel = short_fuel if direction == "short" else long_fuel

    from hunt_core.deliver.dispatch import (
        display_readiness_score,
        geometry_block_reason,
    )

    short_geo = geometry_block_reason(dump, row=row, direction="short")
    long_geo = geometry_block_reason(long_setup, row=row, direction="long")
    short_display = display_readiness_score(dump, direction="short", row=row)
    long_display = display_readiness_score(long_setup, direction="long", row=row)
    suppress_flip = bias == "wait" and phase in _SHORT_BIAS_PHASES

    if direction == "short" and short_geo and not suppress_flip:
        if not long_geo and long_display >= short_display - 15:
            direction = "long"
            setup = long_setup
            fuel = long_fuel
            notes.append(f"SHORT headwind ({short_geo}) — показан LONG")
        elif long_geo and long_display > short_display + 8:
            direction = "long"
            setup = long_setup
            fuel = long_fuel
            notes.append(f"SHORT blocked ({short_geo}); LONG выше по display")
        else:
            notes.append(f"⚠️ SHORT fuel высокий, но {short_geo}")
    elif direction == "short" and short_geo and suppress_flip:
        notes.append(f"dump_active/wait — lean SHORT ({short_geo}), без flip на LONG")
    elif direction == "long" and long_geo:
        if not short_geo and short_display >= long_display - 15:
            direction = "short"
            setup = dump
            fuel = short_fuel
            notes.append(f"LONG headwind ({long_geo}) — показан SHORT")
        elif short_geo and short_display > long_display + 8:
            direction = "short"
            setup = dump
            fuel = short_fuel
            notes.append(f"LONG blocked ({long_geo}); SHORT выше по display")
        else:
            notes.append(f"⚠️ LONG fuel высокий, но {long_geo}")

    return direction, setup, fuel, notes


def probe_header(row: dict[str, Any]) -> tuple[str, str, str]:
    """Telegram /signal header: badge, label, advisory subtitle."""
    lc = row.get("lifecycle") or {}
    bias = str(lc.get("recommended_bias") or "")
    phase = str(lc.get("phase") or "")
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    direction, _, _, _ = resolve_trade_direction(row)

    if phase == "no_setup":
        return (
            "⚖️",
            "НЕТ СЕТАПА",
            "lifecycle=no_setup · направление до структурного confirm не определено",
        )

    if bias == "wait" and phase in _SHORT_BIAS_PHASES:
        return (
            "⏸",
            "MONITOR · lean SHORT",
            "advisory · dump_active · вход только после confirm",
        )
    if bias == "wait":
        lean = "SHORT" if direction == "short" else "LONG"
        return (
            "⏸",
            f"MONITOR · lean {lean}",
            "advisory · bias=wait · без входа до confirm",
        )
    badge = "🔴" if direction == "short" else "🟢"
    label = "SHORT" if direction == "short" else "LONG"
    return badge, label, ""


def correlated_direction(
    *,
    short_fuel: float,
    long_fuel: float,
    btc_corr_1h: float | None,
    btc_trend: str,
    symbol: str = "",
) -> tuple[str, list[str]]:
    """Pick short/long with tiered BTC correlation overlay."""
    from hunt_core.params.store import btc_corr_thresholds

    notes: list[str] = []
    raw = "short" if short_fuel >= long_fuel else "long"
    th = btc_corr_thresholds(symbol)
    soft_min = float(th.get("corr_soft_min", BTC_CORR_SOFT))
    hard_min = float(th.get("corr_hard_min", BTC_CORR_HARD))
    soft_gap = float(th.get("soft_fuel_gap_max", 10.0))
    hard_gap = float(th.get("hard_fuel_gap_max", 18.0))

    if btc_corr_1h is None or btc_trend == "flat":
        notes.append(
            f"без BTC-фильтра (corr={btc_corr_1h if btc_corr_1h is not None else '—'})"
        )
        return raw, notes

    corr = float(btc_corr_1h)
    abs_corr = abs(corr)
    if abs_corr < soft_min:
        notes.append(f"без BTC-фильтра (corr={corr:+.2f} under {soft_min:.2f})")
        return raw, notes
    # Positive corr: alt moves with BTC. Negative: inverse.
    if btc_trend == "up":
        aligned = "long" if corr > 0 else "short"
        contra = "short" if aligned == "long" else "long"
    else:
        aligned = "short" if corr > 0 else "long"
        contra = "long" if aligned == "short" else "short"

    aligned_fuel = short_fuel if aligned == "short" else long_fuel
    raw_fuel = short_fuel if raw == "short" else long_fuel
    fuel_gap = raw_fuel - aligned_fuel

    tier = "hard" if abs_corr >= hard_min else "soft"
    gap_max = hard_gap if tier == "hard" else soft_gap
    notes.append(
        f"BTC {tier} · 1h {btc_trend} · corr={corr:+.2f} → приоритет {aligned.upper()}"
    )
    if raw != aligned and fuel_gap <= gap_max:
        notes.append(
            f"fuel {raw}={raw_fuel:.0f} vs {aligned}={aligned_fuel:.0f} — BTC {tier} bias"
        )
        return aligned, notes
    if raw != aligned:
        notes.append(f"сильный fuel {raw}={raw_fuel:.0f} перекрывает BTC {tier} bias")
    return raw, notes


def scenario_summary(
    *,
    direction: str,
    setup: dict[str, Any],
    fuel: float,
    lc: dict[str, Any],
    confirmed: bool,
    row: dict[str, Any] | None = None,
) -> str:
    """One-line probable development path for Telegram."""
    from hunt_core.deliver.dispatch import (
        display_readiness_score,
        readiness_label_for_setup,
    )

    phase = str(setup.get("phase") or "—")
    lc_phase = str(lc.get("phase") or "")
    readiness = readiness_label_for_setup(
        setup, direction=direction, row=row
    )
    if lc_phase == "no_setup":
        return (
            "⚖️ Нет структурного сетапа — наблюдение без приоритетного направления · "
            f"{readiness}"
        )
    if confirmed:
        hard = setup.get("confirm_hard") or []
        tail = ", ".join(str(h) for h in list(hard)[:3]) or "closed-bar"
        return f"✅ Confirm есть · сценарий: {direction.upper()} по {tail}"
    bias = str(((row or {}).get("lifecycle") or {}).get("recommended_bias") or "")
    display_fuel = display_readiness_score(
        setup, direction=direction, row=row
    )
    if bias == "wait":
        return (
            f"⏸ Monitor · контекст {direction.upper()} ({phase}) · {readiness}"
            " — вход только после confirm"
        )
    if display_fuel >= 60:
        return (
            f"⏳ Ждём confirm · вероятен {direction.upper()} "
            f"({phase}) при закрытии 5m/15m · {readiness}"
        )
    if display_fuel >= 45:
        return (
            f"👀 Формирование · {direction.upper()} {phase} · {readiness}"
            + " — нужен пробой + второй фактор"
        )
    return f"💤 Слабый сетап · {direction.upper()} · {readiness}"


def forming_confirm_gaps(
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    row: dict[str, Any] | None = None,
    price: float = 0.0,
) -> list[str]:
    """Human gaps until closed-bar confirm."""
    from hunt_core.deliver.dispatch import confirm_gap_readiness, readiness_score

    px = price or float((row or {}).get("price") or 0)
    gaps: list[str] = []
    if direction == "short":
        support = local_confirm_level(
            setup, direction="short", row=row, price=px
        )
        r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
        if support > 0 and (r5 <= 0 or r5 >= support):
            tag = local_confirm_label(setup, direction="short", level=support, row=row)
            gaps.append(
                f"5m close below {_fmt_confirm_price(support)} ({tag})"
            )
        score = readiness_score(setup, direction="short")
        if score < 60:
            gaps.append(confirm_gap_readiness(score))
        triggers = list(setup.get("triggers") or [])
        if not any("oi_flush" in t or "lost_support" in t or "div" in t for t in triggers):
            gaps.append("второй фактор (OI/div/continuation)")
    else:
        res = local_confirm_level(setup, direction="long", row=row, price=px)
        r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
        if res > 0 and (r5 <= 0 or r5 <= res):
            tag = local_confirm_label(setup, direction="long", level=res, row=row)
            gaps.append(
                f"5m close above {_fmt_confirm_price(res)} ({tag})"
            )
        score = readiness_score(setup, direction="long")
        if score < 60:
            gaps.append(confirm_gap_readiness(score))
    return gaps


ScenarioDir = Literal["long", "short", "neutral"]


@dataclass(frozen=True, slots=True)
class LiquidityScenario:
    scenario_id: str
    label_ru: str
    direction: ScenarioDir
    probability: float
    path_ru: str
    factors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "label": self.label_ru,
            "direction": self.direction,
            "probability": round(self.probability, 3),
            "path": self.path_ru,
            "factors": list(self.factors),
        }


@dataclass(frozen=True, slots=True)
class LiquidityScenarioPack:
    scenarios: tuple[LiquidityScenario, ...]
    dominant: ScenarioDir
    dominant_probability: float
    context_ru: str
    price: float
    support: float | None
    resistance: float | None
    poc: float | None
    vah: float | None
    val: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dominant": self.dominant,
            "dominant_probability": self.dominant_probability,
            "context": self.context_ru,
            "price": self.price,
            "support": self.support,
            "resistance": self.resistance,
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }


def _f(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _pct_dist(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 999.0
    return abs(a - b) / a * 100.0


def _walls_above_below(
    walls: dict[str, Any],
    price: float,
    *,
    band_pct: float = 0.35,
) -> tuple[float, float, list[str]]:
    """Return (ask_notional_above, bid_notional_below, factor strings)."""
    ask_above = bid_below = 0.0
    factors: list[str] = []
    for lvl in walls.get("ask_levels") or []:
        if not isinstance(lvl, dict):
            continue
        px = _f(lvl.get("price"))
        n = _f(lvl.get("notional_usd"))
        if px is None or n is None or px < price:
            continue
        if _pct_dist(price, px) <= band_pct:
            ask_above += n
    for lvl in walls.get("bid_levels") or []:
        if not isinstance(lvl, dict):
            continue
        px = _f(lvl.get("price"))
        n = _f(lvl.get("notional_usd"))
        if px is None or n is None or px > price:
            continue
        if _pct_dist(price, px) <= band_pct:
            bid_below += n
    if ask_above > 50_000:
        factors.append(f"ликвидность выше ${ask_above/1e3:.0f}k")
    if bid_below > 50_000:
        factors.append(f"ликвидность ниже ${bid_below/1e3:.0f}k")
    return ask_above, bid_below, factors


def _normalize_probs(raw: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, v) for v in raw.values())
    if total <= 0:
        n = len(raw) or 1
        return {k: round(1.0 / n, 3) for k in raw}
    return {k: round(max(0.0, v) / total, 3) for k, v in raw.items()}


def build_liquidity_scenarios(row: dict[str, Any]) -> LiquidityScenarioPack:
    """Rank path scenarios from row snapshot (no extra REST)."""
    price = _f(row.get("price")) or 0.0
    regime = row.get("regime") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    cx = row.get("cross_microstructure") or {}

    vp1h = cx.get("volume_profile_1h") or {}
    poc = _f(vp1h.get("poc")) or _f(regime.get("poc_1h"))
    vah = _f(vp1h.get("vah")) or _f(regime.get("vah_1h"))
    val = _f(vp1h.get("val")) or _f(regime.get("val_1h"))

    support = _f(dump.get("support_break_level")) or _f(lifecycle.get("local_support"))
    resistance = _f(long_setup.get("resistance_break_level")) or _f(
        lifecycle.get("local_resistance")
    )

    walls = cx.get("book_walls") or row.get("book_walls") or {}
    ask_above, bid_below, wall_factors = _walls_above_below(walls, price)

    near_support = support is not None and _pct_dist(support, price) <= 0.45
    near_resistance = resistance is not None and _pct_dist(resistance, price) <= 0.45
    poc_above = poc is not None and poc > price * 1.002
    poc_below = poc is not None and poc < price * 0.998

    long_stop = _f(long_setup.get("stop_loss"))
    short_stop = _f(dump.get("stop_loss"))
    stop_below_support = (
        support is not None
        and long_stop is not None
        and long_stop < support * 0.999
    )
    stop_above_resistance = (
        resistance is not None
        and short_stop is not None
        and short_stop > resistance * 1.001
    )

    raw: dict[str, float] = {
        "sweep_support_to_poc": 0.0,
        "breakdown_support": 0.0,
        "sweep_resistance_to_poc": 0.0,
        "breakout_resistance": 0.0,
        "range_poc_magnet": 0.0,
        "continuation_htf": 0.0,
    }

    # Long-bias: sweep stops under support, then mean-revert toward POC above.
    if near_support and poc_above:
        raw["sweep_support_to_poc"] += 0.35
        if stop_below_support:
            raw["sweep_support_to_poc"] += 0.25
        if ask_above > bid_below:
            raw["sweep_support_to_poc"] += 0.12
        if bid_below > 0:
            raw["sweep_support_to_poc"] += 0.08

    if near_support and poc_below:
        raw["breakdown_support"] += 0.30
        if ask_above > bid_below * 1.5:
            raw["breakdown_support"] += 0.20

    if near_resistance and poc_below:
        raw["sweep_resistance_to_poc"] += 0.35
        if stop_above_resistance:
            raw["sweep_resistance_to_poc"] += 0.25
        if bid_below > ask_above:
            raw["sweep_resistance_to_poc"] += 0.12

    if near_resistance and poc_above:
        raw["breakout_resistance"] += 0.28
        if ask_above < bid_below:
            raw["breakout_resistance"] += 0.15

    if poc is not None and _pct_dist(poc, price) <= 0.25:
        raw["range_poc_magnet"] += 0.20
    elif poc is not None and min(_pct_dist(poc, price), 99) <= 1.2:
        raw["range_poc_magnet"] += 0.12

    mtf = row.get("mtf")
    dom = getattr(mtf, "dominant", None) if mtf else None
    if dom == "short":
        raw["continuation_htf"] += 0.22
    elif dom == "long":
        raw["continuation_htf"] += 0.22

    probs = _normalize_probs(raw)

    scenarios: list[LiquidityScenario] = []

    if probs["sweep_support_to_poc"] > 0.05:
        factors = [
            "цена у поддержки",
            "POC выше текущей цены",
            *wall_factors,
        ]
        if stop_below_support:
            factors.append("стоп ниже поддержки — типичная зона sweep")
        scenarios.append(
            LiquidityScenario(
                scenario_id="sweep_support_to_poc",
                label_ru="Sweep поддержки → отскок к POC",
                direction="long",
                probability=probs["sweep_support_to_poc"],
                path_ru=(
                    "Цена может сходить ниже поддержки (снять стопы), "
                    "затем вернуться к POC — магнит объёма выше."
                ),
                factors=tuple(factors),
            )
        )

    if probs["breakdown_support"] > 0.05:
        scenarios.append(
            LiquidityScenario(
                scenario_id="breakdown_support",
                label_ru="Пробой поддержки",
                direction="short",
                probability=probs["breakdown_support"],
                path_ru="Удержание ниже поддержки — движение к POC ниже или VAL.",
                factors=("POC ниже цены", "продавцы доминируют у поддержки"),
            )
        )

    if probs["sweep_resistance_to_poc"] > 0.05:
        factors = ["цена у сопротивления", "POC ниже"]
        if stop_above_resistance:
            factors.append("стоп выше сопротивления — зона sweep шортов")
        scenarios.append(
            LiquidityScenario(
                scenario_id="sweep_resistance_to_poc",
                label_ru="Sweep сопротивления → откат к POC",
                direction="short",
                probability=probs["sweep_resistance_to_poc"],
                path_ru=(
                    "Возможен вынос выше сопротивления (ликвидность шортов), "
                    "затем снижение к POC."
                ),
                factors=tuple(factors),
            )
        )

    if probs["breakout_resistance"] > 0.05:
        scenarios.append(
            LiquidityScenario(
                scenario_id="breakout_resistance",
                label_ru="Пробой сопротивления",
                direction="long",
                probability=probs["breakout_resistance"],
                path_ru="Закрепление выше сопротивления — движение к POC/VAH выше.",
                factors=("POC выше",),
            )
        )

    if probs["range_poc_magnet"] > 0.05:
        scenarios.append(
            LiquidityScenario(
                scenario_id="range_poc_magnet",
                label_ru="Боковик вокруг POC",
                direction="neutral",
                probability=probs["range_poc_magnet"],
                path_ru="Цена в зоне POC — вероятен боковик / магнит объёма.",
                factors=(f"POC {_fmt(poc)}",),
            )
        )

    if probs["continuation_htf"] > 0.05:
        dir_ru = "нисходящее" if dom == "short" else "восходящее"
        scenarios.append(
            LiquidityScenario(
                scenario_id="continuation_htf",
                label_ru=f"Продолжение HTF ({dir_ru})",
                direction=str(dom) if dom in {"long", "short"} else "neutral",
                probability=probs["continuation_htf"],
                path_ru=f"Старший ТФ доминирует — приоритет {dir_ru} движения.",
                factors=(f"MTF bias: {dom}",),
            )
        )

    scenarios.sort(key=lambda s: s.probability, reverse=True)
    if not scenarios:
        scenarios = [
            LiquidityScenario(
                scenario_id="insufficient_data",
                label_ru="Недостаточно уровней",
                direction="neutral",
                probability=1.0,
                path_ru="Нет POC/поддержки/стакана для сценария.",
                factors=(),
            )
        ]

    dom_sc = scenarios[0]
    ctx_parts: list[str] = []
    if support:
        ctx_parts.append(f"support {_fmt(support)}")
    if poc:
        ctx_parts.append(f"POC {_fmt(poc)}")
    if resistance:
        ctx_parts.append(f"res {_fmt(resistance)}")

    return LiquidityScenarioPack(
        scenarios=tuple(scenarios[:5]),
        dominant=dom_sc.direction,
        dominant_probability=dom_sc.probability,
        context_ru=" · ".join(ctx_parts) if ctx_parts else "—",
        price=price,
        support=support,
        resistance=resistance,
        poc=poc,
        vah=vah,
        val=val,
    )


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:.2f}"
    return f"{v:.5f}"


def apply_liquidity_to_mtf_scores(
    long_score: float,
    short_score: float,
    pack: LiquidityScenarioPack | None,
) -> tuple[float, float, list[str]]:
    """Adjust MTF scenario scores using dominant liquidity path."""
    if pack is None or not pack.scenarios:
        return long_score, short_score, []
    notes: list[str] = []
    boost = min(0.18, pack.dominant_probability * 0.25)
    top = pack.scenarios[0]
    if top.direction == "long":
        long_score = min(1.0, long_score + boost)
        short_score = max(0.0, short_score - boost * 0.35)
        notes.append(f"ликвидность: {top.label_ru} ({top.probability:.0%})")
    elif top.direction == "short":
        short_score = min(1.0, short_score + boost)
        long_score = max(0.0, long_score - boost * 0.35)
        notes.append(f"ликвидность: {top.label_ru} ({top.probability:.0%})")
    else:
        long_score = max(0.0, long_score - boost * 0.2)
        short_score = max(0.0, short_score - boost * 0.2)
        notes.append(f"ликвидность: {top.label_ru} — без явного edge")
    return round(long_score, 3), round(short_score, 3), notes


def format_liquidity_scenarios_telegram(pack: LiquidityScenarioPack | dict[str, Any]) -> str:
    if isinstance(pack, dict):
        scenarios_raw = pack.get("scenarios") or []
        lines = ["🧲 <b>Сценарии ликвидности</b>"]
        if pack.get("context"):
            lines.append(f"<i>{pack['context']}</i>")
        for sc in scenarios_raw[:3]:
            if not isinstance(sc, dict):
                continue
            prob = float(sc.get("probability") or 0) * 100
            lines.append(
                f"• <b>{sc.get('label', '?')}</b> — {prob:.0f}%"
            )
            path = sc.get("path")
            if path:
                lines.append(f"  <i>{path}</i>")
        return "\n".join(lines) if len(lines) > 1 else ""

    lines = ["🧲 <b>Сценарии ликвидности</b>", f"<i>{pack.context_ru}</i>"]
    for sc in pack.scenarios[:3]:
        lines.append(f"• <b>{sc.label_ru}</b> — {sc.probability * 100:.0f}%")
        lines.append(f"  <i>{sc.path_ru}</i>")
    return "\n".join(lines)





from hunt_core.confluence.mtf import (
    MTFConfluence,
    ScenarioScore,
    TFSignal,
    build_mtf_confluence,
)




TrendDir = Literal["bull", "bear", "flat"]
AbsorptionKind = Literal["bid_absorption", "ask_absorption", "none"]


@dataclass(frozen=True, slots=True)
class OrderFlowSynthesis:
    cvd_trend: TrendDir
    cvd_note_ru: str
    absorption: AbsorptionKind
    absorption_note_ru: str
    aggressor: str
    aggressor_note_ru: str
    delta_30s: float | None
    delta_60s: float | None
    taker_5m: float | None
    depth_imbalance: float | None
    summary_ru: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cvd_trend": self.cvd_trend,
            "cvd_note": self.cvd_note_ru,
            "absorption": self.absorption,
            "absorption_note": self.absorption_note_ru,
            "aggressor": self.aggressor,
            "aggressor_note": self.aggressor_note_ru,
            "delta_30s": self.delta_30s,
            "delta_60s": self.delta_60s,
            "taker_5m": self.taker_5m,
            "depth_imbalance": self.depth_imbalance,
            "summary": self.summary_ru,
        }


def _cvd_from_tf(row: dict[str, Any]) -> tuple[float | None, float | None]:
    tf = row.get("timeframes") or {}
    for key in ("1h", "15m", "5m"):
        snap = tf.get(key) or {}
        cur = _f(snap.get("session_cvd") or snap.get("rolling_cvd_24h"))
        prev = _f(snap.get("session_cvd_prev") or snap.get("cvd_prev"))
        if cur is not None:
            return cur, prev
    return None, None


def _infer_cvd_trend(cur: float | None, prev: float | None) -> tuple[TrendDir, str]:
    if cur is None:
        return "flat", "CVD недоступен"
    if prev is None:
        if cur > 0:
            return "bull", f"CVD положительный ({cur:+.0f})"
        if cur < 0:
            return "bear", f"CVD отрицательный ({cur:+.0f})"
        return "flat", "CVD ≈ 0"
    delta = cur - prev
    if delta > abs(cur) * 0.02 or delta > 500:
        return "bull", f"CVD растёт ({cur:+.0f}, Δ{delta:+.0f})"
    if delta < -abs(cur) * 0.02 or delta < -500:
        return "bear", f"CVD падает ({cur:+.0f}, Δ{delta:+.0f})"
    return "flat", f"CVD боковой ({cur:+.0f})"


def _infer_absorption(
    *,
    depth_imb: float | None,
    delta_30s: float | None,
    taker_5m: float | None,
) -> tuple[AbsorptionKind, str]:
    if depth_imb is None or delta_30s is None:
        return "none", ""
    # Bid-heavy book + positive delta but price stalling → bid absorption (bullish)
    if depth_imb >= 0.12 and delta_30s > 0 and (taker_5m or 0) < 0.52:
        return "bid_absorption", "Поглощение на bid — продавцы не давят цену вниз"
    if depth_imb <= -0.12 and delta_30s < 0 and (taker_5m or 1) > 0.48:
        return "ask_absorption", "Поглощение на ask — покупатели не поднимают цену"
    return "none", ""


def _infer_aggressor(
    *,
    taker_5m: float | None,
    delta_30s: float | None,
    delta_60s: float | None,
) -> tuple[str, str]:
    if taker_5m is not None:
        if taker_5m >= 0.58:
            return "buyers", f"Агрессор: покупатели (taker {taker_5m:.2f})"
        if taker_5m <= 0.42:
            return "sellers", f"Агрессор: продавцы (taker {taker_5m:.2f})"
    if delta_60s is not None:
        if delta_60s > 0:
            return "buyers", f"Δ60s buy-heavy ({delta_60s:+.0f})"
        if delta_60s < 0:
            return "sellers", f"Δ60s sell-heavy ({delta_60s:+.0f})"
    if delta_30s is not None:
        if delta_30s > 0:
            return "buyers", f"Δ30s buy ({delta_30s:+.0f})"
        if delta_30s < 0:
            return "sellers", f"Δ30s sell ({delta_30s:+.0f})"
    return "balanced", "Агрессор сбалансирован"


def synthesize_order_flow(row: dict[str, Any]) -> OrderFlowSynthesis:
    """Build CVD / absorption / aggressor summary from tick row + market block."""
    market = row.get("market") or {}
    delta_30s = _f(market.get("agg_trade_delta_30s"))
    delta_60s = _f(market.get("agg_trade_delta_60s"))
    taker_5m = _f(market.get("taker_5m"))
    depth_imb = _f(market.get("depth_imbalance"))

    cur, prev = _cvd_from_tf(row)
    cvd_trend, cvd_note = _infer_cvd_trend(cur, prev)
    absorption, abs_note = _infer_absorption(
        depth_imb=depth_imb, delta_30s=delta_30s, taker_5m=taker_5m
    )
    aggressor, aggr_note = _infer_aggressor(
        taker_5m=taker_5m, delta_30s=delta_30s, delta_60s=delta_60s
    )

    parts = [p for p in (cvd_note, abs_note, aggr_note) if p]
    summary = " · ".join(parts) if parts else "Order flow нейтральный"

    return OrderFlowSynthesis(
        cvd_trend=cvd_trend,
        cvd_note_ru=cvd_note,
        absorption=absorption,
        absorption_note_ru=abs_note,
        aggressor=aggressor,
        aggressor_note_ru=aggr_note,
        delta_30s=delta_30s,
        delta_60s=delta_60s,
        taker_5m=taker_5m,
        depth_imbalance=depth_imb,
        summary_ru=summary,
    )


def format_order_flow_block(synth: OrderFlowSynthesis | dict[str, Any]) -> str:
    import html

    if isinstance(synth, dict):
        summary = str(synth.get("summary") or synth.get("summary_ru") or "")
        cvd = str(synth.get("cvd_note") or synth.get("cvd_note_ru") or "")
        abs_n = str(synth.get("absorption_note") or synth.get("absorption_note_ru") or "")
        aggr = str(synth.get("aggressor_note") or synth.get("aggressor_note_ru") or "")
    else:
        summary, cvd, abs_n, aggr = synth.summary_ru, synth.cvd_note_ru, synth.absorption_note_ru, synth.aggressor_note_ru
    lines = ["🌊 <b>ORDER FLOW</b>"]
    if summary:
        lines.append(html.escape(summary))
    for bit in (cvd, abs_n, aggr):
        if bit and bit not in summary:
            lines.append(f"· {html.escape(bit)}")
    return "\n".join(lines)




ScenarioDir = Literal["long", "short", "neutral", "watch"]
AnalysisMode = Literal["deep_analysis", "hunt_scan"]


@dataclass(frozen=True, slots=True)
class PocLevelScenario:
    scenario_id: str
    label_ru: str
    direction: ScenarioDir
    confidence: float
    action_ru: str
    factors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "label": self.label_ru,
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "action": self.action_ru,
            "factors": list(self.factors),
        }


@dataclass(frozen=True, slots=True)
class PocLevelScenarioPack:
    """Ranked POC interaction scenarios for Telegram deep block."""

    scenarios: tuple[PocLevelScenario, ...]
    primary: PocLevelScenario | None
    level_price: float | None
    level_label: str
    level_tf: str
    dist_pct: float | None
    poc_direction: str
    context_ru: str
    mode: AnalysisMode = "deep_analysis"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "level_price": self.level_price,
            "level_label": self.level_label,
            "level_tf": self.level_tf,
            "dist_pct": self.dist_pct,
            "poc_direction": self.poc_direction,
            "context": self.context_ru,
            "primary": self.primary.to_dict() if self.primary else None,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }


def is_deep_analysis_context(row: dict[str, Any]) -> bool:
    """True when row is pinned anchor tick, explicit ``/signal``, or ``/signals`` catalog."""
    if row.get("_deep_analysis") or row.get("_pinned_reference") or row.get("_signals_catalog"):
        return True
    sym = str(row.get("symbol") or "").strip().upper()
    from hunt_core.analysis.pinned_deep import is_pinned_symbol

    return is_pinned_symbol(sym)


def _candle_close(block: dict[str, Any] | None) -> float | None:
    if not isinstance(block, dict):
        return None
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    return _f(candle.get("close") or block.get("close"))


def _closes_beyond(
    tf: dict[str, Any],
    level: float,
    *,
    side: Literal["above", "below"],
    keys: tuple[str, ...] = ("15m_closed", "1h_closed"),
) -> int:
    count = 0
    mult = 1.001 if side == "above" else 0.999
    for key in keys:
        close = _candle_close(tf.get(key))
        if close is None:
            continue
        if side == "above" and close > level * mult:
            count += 1
        elif side == "below" and close < level * mult:
            count += 1
    return count


def _resolve_level(row: dict[str, Any]) -> tuple[float | None, str, str, str]:
    """Return (price, label, tf, poc_direction)."""
    regime = row.get("regime") or {}
    cx = row.get("cross_microstructure") or {}
    vp1h = cx.get("volume_profile_1h") if isinstance(cx.get("volume_profile_1h"), dict) else {}
    poc = _f(vp1h.get("poc")) or _f(regime.get("poc_1h"))
    poc_dir = str(regime.get("poc_direction_1h") or "")
    if poc:
        return poc, "POC 1h", "1h", poc_dir

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lifecycle = row.get("lifecycle") or {}
    price = _f(row.get("price")) or 0.0
    res = _f(long_setup.get("resistance_break_level")) or _f(lifecycle.get("local_resistance"))
    sup = _f(dump.get("support_break_level")) or _f(lifecycle.get("local_support"))
    if res and price and (not sup or _pct_dist(price, res) <= _pct_dist(price, sup)):
        return res, "сопротивление", "15m", poc_dir
    if sup:
        return sup, "поддержка", "15m", poc_dir
    return None, "—", "15m", poc_dir


def _empty_pack(mode: AnalysisMode = "hunt_scan") -> PocLevelScenarioPack:
    return PocLevelScenarioPack(
        scenarios=(),
        primary=None,
        level_price=None,
        level_label="—",
        level_tf="—",
        dist_pct=None,
        poc_direction="",
        context_ru="режим охоты — POC-сценарии не строятся",
        mode=mode,
    )


def _attach_structure_signals(row: dict[str, Any]) -> None:
    """PP (переприор) + chart patterns from prepared work frames."""
    prepared = row.get("_prepared")
    if prepared is None:
        return
    from hunt_core.features.structure import detect_pp
    from hunt_core.features.chart_patterns import chart_pattern_snapshot

    pp: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    for label, attr in (("1h", "work_1h"), ("15m", "work_15m")):
        work = getattr(prepared, attr, None)
        if work is None or getattr(work, "is_empty", lambda: True)():
            continue
        pp[label] = detect_pp(work, closed=True)
        charts[label] = chart_pattern_snapshot(work)
    if pp:
        row["pp_signals"] = pp
    if charts:
        row["chart_patterns"] = charts


def _pp_boost(raw: dict[str, float], pp: dict[str, Any]) -> None:
    """Boost Prizrak scenario weights from PP break state."""
    for tf_pp in pp.values():
        if not isinstance(tf_pp, dict):
            continue
        if tf_pp.get("pp_short_true"):
            raw["prizrak_04_break_retest"] += 0.22
            raw["prizrak_06_grind_weak"] += 0.10
        elif tf_pp.get("pp_short_early"):
            raw["prizrak_04_break_retest"] += 0.12
            raw["prizrak_03_trap_flip"] += 0.08
        if tf_pp.get("pp_long_true"):
            raw["prizrak_04_break_retest"] += 0.22
            raw["prizrak_01_reaction"] += 0.10
        elif tf_pp.get("pp_long_early"):
            raw["prizrak_04_break_retest"] += 0.12
            raw["prizrak_01_reaction"] += 0.06


def _chart_boost(raw: dict[str, float], charts: dict[str, Any]) -> None:
    for snap in charts.values():
        if not isinstance(snap, dict):
            continue
        db = snap.get("double_bottom")
        if isinstance(db, dict) and db.get("pattern"):
            raw["prizrak_01_reaction"] += 0.15 * float(db.get("confidence") or 0)
            raw["prizrak_04_break_retest"] += 0.08 * float(db.get("confidence") or 0)
        hs = snap.get("head_and_shoulders")
        if isinstance(hs, dict) and hs.get("pattern"):
            raw["prizrak_03_trap_flip"] += 0.12 * float(hs.get("confidence") or 0)
            raw["prizrak_06_grind_weak"] += 0.10 * float(hs.get("confidence") or 0)


def build_poc_level_scenarios(row: dict[str, Any]) -> PocLevelScenarioPack | None:
    """Classify Prizrak 7 level-interaction patterns. Returns None outside deep analysis."""
    if not is_deep_analysis_context(row):
        return None

    _attach_structure_signals(row)
    pp_signals = row.get("pp_signals") if isinstance(row.get("pp_signals"), dict) else {}
    chart_signals = row.get("chart_patterns") if isinstance(row.get("chart_patterns"), dict) else {}

    price = _f(row.get("price")) or 0.0
    if price <= 0:
        return _empty_pack()

    level, level_label, level_tf, poc_dir = _resolve_level(row)
    if level is None:
        return PocLevelScenarioPack(
            scenarios=(),
            primary=None,
            level_price=None,
            level_label="—",
            level_tf="—",
            dist_pct=None,
            poc_direction=poc_dir,
            context_ru="нет POC/уровня для классификации",
            mode="deep_analysis",
        )

    tf = row.get("timeframes") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc_phase = str(lifecycle.get("phase") or "")
    dist = _pct_dist(price, level)

    from hunt_core.gate.delivery import detect_prokol

    prokol_long = detect_prokol(level=level, break_direction="long", tf=tf)
    prokol_short = detect_prokol(level=level, break_direction="short", tf=tf)
    level_expired = bool(dump.get("level_expired") or long_setup.get("level_expired"))
    closes_above = _closes_beyond(tf, level, side="above")
    closes_below = _closes_beyond(tf, level, side="below")
    near_level = dist <= 0.55
    retest_from_above = closes_above >= 1 and near_level and price <= level * 1.004
    retest_from_below = closes_below >= 1 and near_level and price >= level * 0.996
    chop_phase = lc_phase in {
        "accumulation",
        "accumulation_watch",
        "distribution",
        "exhaustion_watch",
        "no_setup",
    }

    raw: dict[str, float] = {
        "prizrak_01_reaction": 0.0,
        "prizrak_02_base_shift": 0.0,
        "prizrak_03_trap_flip": 0.0,
        "prizrak_04_break_retest": 0.0,
        "prizrak_05_base_on_break": 0.0,
        "prizrak_06_grind_weak": 0.0,
        "prizrak_07_saw": 0.0,
    }

    # 1 — reaction from POC (prokol OK if reclaimed = reaction, not trap continuation)
    if near_level or dist <= 1.0:
        raw["prizrak_01_reaction"] += 0.22
        if poc_dir == "long" and price >= level * 0.997:
            raw["prizrak_01_reaction"] += 0.18
        if poc_dir == "short" and price <= level * 1.003:
            raw["prizrak_01_reaction"] += 0.18
        if prokol_long.get("prokol") or prokol_short.get("prokol"):
            raw["prizrak_01_reaction"] += 0.12

    # 2 — new accumulation below/above level
    if chop_phase and dist <= 1.8:
        raw["prizrak_02_base_shift"] += 0.25
        if closes_below >= 1 and price > level:
            raw["prizrak_02_base_shift"] += 0.15
        if closes_above >= 1 and price < level:
            raw["prizrak_02_base_shift"] += 0.15

    # 3 — trap: false break + flip (prokol / tf_trap)
    if prokol_long.get("prokol") or prokol_short.get("prokol"):
        raw["prizrak_03_trap_flip"] += 0.35
        if prokol_long.get("tf_trap") or prokol_short.get("tf_trap"):
            raw["prizrak_03_trap_flip"] += 0.20

    # 4 — confirmed break + retest from other side
    if retest_from_above or retest_from_below:
        raw["prizrak_04_break_retest"] += 0.32
        if closes_above >= 2 or closes_below >= 2:
            raw["prizrak_04_break_retest"] += 0.18

    # 5 — base on broken level (accumulation at flip zone)
    if chop_phase and (closes_above >= 1 or closes_below >= 1) and dist <= 1.2:
        raw["prizrak_05_base_on_break"] += 0.28
        if lc_phase in {"accumulation", "distribution"}:
            raw["prizrak_05_base_on_break"] += 0.12

    # 6 — level weakened after first touch
    if level_expired:
        raw["prizrak_06_grind_weak"] += 0.45
    elif dist <= 0.35 and any("poc_aligned" in str(t) for t in (dump.get("triggers") or [])):
        raw["prizrak_06_grind_weak"] += 0.15

    # 7 — saw at level (chop on POC, no clean break)
    if near_level and chop_phase and closes_above == 0 and closes_below == 0:
        raw["prizrak_07_saw"] += 0.30
    if near_level and prokol_long.get("prokol") and prokol_short.get("prokol"):
        raw["prizrak_07_saw"] += 0.25

    if pp_signals:
        _pp_boost(raw, pp_signals)
    if chart_signals:
        _chart_boost(raw, chart_signals)

    probs = _normalize_probs(raw)

    catalog: dict[str, tuple[str, ScenarioDir, str]] = {
        "prizrak_01_reaction": (
            "① Реакция от уровня",
            "long" if poc_dir == "long" else "short" if poc_dir == "short" else "neutral",
            "Ждём отскок от POC; прокол допустим — вход только после confirm closed-bar.",
        ),
        "prizrak_02_base_shift": (
            "② Накопление под/над уровнем",
            "watch",
            "Сформировалась база у уровня — приоритет по HTF; не лимитить до выхода из базы.",
        ),
        "prizrak_03_trap_flip": (
            "③ Ловушка — переворот уровня",
            "watch",
            "Ложный пробой: закрыть контр-тренд в БУ; новый сценарий — retest с обратной стороны.",
        ),
        "prizrak_04_break_retest": (
            "④ Пробой + retest",
            "long" if retest_from_above else "short" if retest_from_below else "watch",
            "Уровень закреплён 2+ свечами — retest с обратной стороны = новая ТВХ по тренду пробоя.",
        ),
        "prizrak_05_base_on_break": (
            "⑤ База на пробитом уровне",
            "long" if closes_above >= closes_below else "short",
            "Накопление на сломанном уровне — вход от границы базы после выхода + retest.",
        ),
        "prizrak_06_grind_weak": (
            "⑥ Уровень отработан — слабее",
            "neutral",
            "Первое касание отработано; лимитные ордера на этом POC не ставим — ищем новый уровень.",
        ),
        "prizrak_07_saw": (
            "⑦ Пила на уровне",
            "watch",
            "Цена «пилит» POC с двух сторон — выйти в БУ, ждать выход из базы и retest.",
        ),
    }

    scenarios: list[PocLevelScenario] = []
    for sid, prob in sorted(probs.items(), key=lambda x: x[1], reverse=True):
        if prob < 0.08:
            continue
        label, default_dir, action = catalog[sid]
        factors: list[str] = []
        if dist < 999:
            factors.append(f"dist {dist:.2f}%")
        if poc_dir:
            factors.append(f"POC bias {poc_dir}")
        if prokol_long.get("tf_trap") or prokol_short.get("tf_trap"):
            factors.append("ловушка TF")
        if level_expired:
            factors.append("level_expired")
        pp = row.get("pp_signals") if isinstance(row.get("pp_signals"), dict) else {}
        for tf_pp in pp.values():
            if isinstance(tf_pp, dict) and (
                tf_pp.get("pp_short_true")
                or tf_pp.get("pp_long_true")
                or tf_pp.get("pp_short_early")
                or tf_pp.get("pp_long_early")
            ):
                factors.append("ПП")
                break
        scenarios.append(
            PocLevelScenario(
                scenario_id=sid,
                label_ru=label,
                direction=default_dir,
                confidence=prob,
                action_ru=action,
                factors=tuple(factors),
            )
        )

    primary = scenarios[0] if scenarios else None
    ctx = f"{level_label} {_fmt(level)} · {level_tf}"
    if dist < 999:
        ctx = f"{ctx} · dist {dist:.2f}%"

    pack = PocLevelScenarioPack(
        scenarios=tuple(scenarios[:4]),
        primary=primary,
        level_price=level,
        level_label=level_label,
        level_tf=level_tf,
        dist_pct=round(dist, 3) if dist < 999 else None,
        poc_direction=poc_dir,
        context_ru=ctx,
        mode="deep_analysis",
    )
    row["poc_level_scenarios"] = pack
    return pack


def format_poc_level_scenarios_telegram(
    pack: PocLevelScenarioPack | dict[str, Any] | None,
) -> str:
    """Telegram block for deep ``/signal`` only."""
    if pack is None:
        return ""
    if isinstance(pack, dict):
        if pack.get("mode") == "hunt_scan":
            return ""
        primary = pack.get("primary")
        scenarios = pack.get("scenarios") or []
        ctx = str(pack.get("context") or "")
        lines = ["📍 <b>Сценарий уровня</b> <i>(deep · Prizrak)</i>"]
        if ctx:
            lines.append(f"<i>{ctx}</i>")
        if isinstance(primary, dict):
            conf = float(primary.get("confidence") or 0) * 100
            lines.append(
                f"★ <b>{primary.get('label', '?')}</b> — {conf:.0f}%"
            )
            act = primary.get("action")
            if act:
                lines.append(f"  {act}")
        for sc in scenarios[1:3]:
            if not isinstance(sc, dict):
                continue
            conf = float(sc.get("confidence") or 0) * 100
            lines.append(f"• {sc.get('label', '?')} — {conf:.0f}%")
        return "\n".join(lines) if len(lines) > 1 else ""

    if pack.mode != "deep_analysis" or not pack.scenarios:
        return ""

    lines = [
        "📍 <b>Сценарий уровня</b> <i>(deep · Prizrak)</i>",
        f"<i>{pack.context_ru}</i>",
    ]
    primary = pack.primary
    if primary:
        lines.append(
            f"★ <b>{primary.label_ru}</b> — {primary.confidence * 100:.0f}%"
        )
        lines.append(f"  {primary.action_ru}")
    for sc in pack.scenarios[1:3]:
        lines.append(f"• {sc.label_ru} — {sc.confidence * 100:.0f}%")
    lines.append(
        "<i>Pinned: обновляется каждый tick · memecoin scan — только fuel+confirm.</i>"
    )
    return "\n".join(lines)

__all__ = [
    "LiquidityScenario",
    "LiquidityScenarioPack",
    "apply_liquidity_to_mtf_scores",
    "build_liquidity_scenarios",
    "format_liquidity_scenarios_telegram",
    "OrderFlowSynthesis",
    "format_order_flow_block",
    "synthesize_order_flow",
    "PocLevelScenario",
    "PocLevelScenarioPack",
    "build_poc_level_scenarios",
    "format_poc_level_scenarios_telegram",
    "is_deep_analysis_context",
]
