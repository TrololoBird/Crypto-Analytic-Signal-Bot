"""Data-plane strategy pools for shortlist assembly (v2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.domain.config import BotSettings
    from bot.domain.schemas import UniverseSymbol

# Primary public data plane per setup (signal-only, no private API).
SETUP_DATA_POOL: dict[str, str] = {
    "structure_pullback": "klines",
    "structure_break_retest": "klines",
    "wick_trap_reversal": "klines",
    "squeeze_setup": "klines",
    "ema_bounce": "klines",
    "fvg_setup": "klines",
    "order_block": "klines",
    "liquidity_sweep": "klines",
    "bos_choch": "klines",
    "hidden_divergence": "klines",
    "indicator_divergence": "orderflow",
    "funding_reversal": "positioning",
    "cvd_divergence": "orderflow",
    "session_killzone": "klines",
    "breaker_block": "klines",
    "turtle_soup": "klines",
    "vwap_trend": "klines",
    "supertrend_follow": "klines",
    "multi_tf_trend": "klines",
    "price_velocity": "klines",
    "volume_anomaly": "klines",
    "volume_climax_reversal": "klines",
    "keltner_breakout": "klines",
    "bb_squeeze": "klines",
    "atr_expansion": "klines",
    "whale_walls": "orderbook",
    "spread_strategy": "orderbook",
    "depth_imbalance": "orderbook",
    "absorption": "orderflow",
    "aggression_shift": "orderflow",
    "liquidation_heatmap": "positioning",
    "stop_hunt_detection": "klines",
    "oi_divergence": "positioning",
    "ls_ratio_extreme": "positioning",
    "rsi_divergence_bottom": "klines",
    "wyckoff_spring": "klines",
    "btc_correlation": "multi_asset",
    "altcoin_season_index": "multi_asset",
}

DATA_POOL_SETUPS: dict[str, frozenset[str]] = {
    pool: frozenset(setup for setup, p in SETUP_DATA_POOL.items() if p == pool)
    for pool in ("klines", "positioning", "orderbook", "orderflow", "multi_asset")
}

RESERVED_PER_SETUP = 2
MAX_PER_DECORRELATION_KEY = 2


def decorrelation_key(item: UniverseSymbol) -> str:
    """Proxy cluster: liquidity tier + 1% move bucket (anti clone shortlist)."""
    move_bucket = int(round(abs(float(item.price_change_pct)) / 1.0))
    if (item.liquidity_rank or 999) <= 10:
        tier = "mega"
    elif (item.liquidity_rank or 999) <= 30:
        tier = "mid"
    else:
        tier = "tail"
    return f"{tier}:{move_bucket}"


def row_passes_pool_gates(row: dict[str, Any], pool: str) -> bool:
    """Return True when REST/WS row has minimum fields for a data pool."""
    if pool == "klines":
        return True
    if pool == "positioning":
        oi = row.get("oi_current")
        funding = row.get("funding_rate")
        return oi is not None or funding is not None
    if pool == "orderbook":
        spread = row.get("spread_bps")
        return spread is not None and float(spread or 0.0) > 0.0
    if pool == "orderflow":
        taker = row.get("taker_ratio")
        liq = row.get("liquidation_score")
        return taker is not None or liq is not None
    if pool == "multi_asset":
        return True
    return True


def scaled_pool_targets(
    remaining_slots: int,
    *,
    market_regime: str | None = None,
) -> dict[str, int]:
    """Allocate dynamic slots across data pools."""
    if remaining_slots <= 0:
        return dict.fromkeys(DATA_POOL_SETUPS, 0)
    base = {
        "klines": 18,
        "positioning": 10,
        "orderbook": 6,
        "orderflow": 5,
        "multi_asset": 3,
    }
    regime = str(market_regime or "").strip().lower()
    if regime in {"bear", "decline", "risk_off"}:
        base = {"klines": 12, "positioning": 14, "orderbook": 6, "orderflow": 6, "multi_asset": 4}
    elif regime in {"bull", "expansion", "uptrend"}:
        base = {"klines": 20, "positioning": 8, "orderbook": 6, "orderflow": 4, "multi_asset": 4}
    total = sum(base.values())
    scaled = {key: max(0, round(remaining_slots * weight / total)) for key, weight in base.items()}
    assigned = sum(scaled.values())
    priority = ("klines", "positioning", "orderbook", "orderflow", "multi_asset")
    idx = 0
    while assigned < remaining_slots:
        key = priority[idx % len(priority)]
        scaled[key] += 1
        assigned += 1
        idx += 1
    while assigned > remaining_slots:
        for key in reversed(priority):
            if scaled[key] > 0:
                scaled[key] -= 1
                assigned -= 1
                if assigned <= remaining_slots:
                    break
    return scaled


def _candidate_sort_key(item: UniverseSymbol) -> tuple[float, float, str]:
    return (
        float(item.shortlist_score or 0.0),
        float(item.quote_volume),
        item.symbol,
    )


def fill_shortlist_from_pools(
    *,
    shortlist: list[UniverseSymbol],
    seen: set[str],
    dynamic_pool: list[UniverseSymbol],
    shortlist_limit: int,
    setup_ids: tuple[str, ...],
    market_regime: str | None = None,
) -> dict[str, Any]:
    """Grow shortlist via set-cover over data pools + per-setup reserved slots."""
    summary: dict[str, Any] = {
        "pool_klines": 0,
        "pool_positioning": 0,
        "pool_orderbook": 0,
        "pool_orderflow": 0,
        "pool_multi_asset": 0,
        "strategy_seed": 0,
        "fill": 0,
        "decorrelation_skips": 0,
    }
    decor_counts: dict[str, int] = {}
    for existing in shortlist:
        key = decorrelation_key(existing)
        decor_counts[key] = decor_counts.get(key, 0) + 1

    remaining = max(shortlist_limit - len(shortlist), 0)
    pool_targets = scaled_pool_targets(remaining, market_regime=market_regime)

    def decor_available(item: UniverseSymbol) -> bool:
        key = decorrelation_key(item)
        return decor_counts.get(key, 0) < MAX_PER_DECORRELATION_KEY

    def can_add(item: UniverseSymbol) -> bool:
        return item.symbol not in seen and bool(item.strategy_fits) and decor_available(item)

    def add_item(item: UniverseSymbol, pool_name: str) -> None:
        shortlist.append(item)
        seen.add(item.symbol)
        key = decorrelation_key(item)
        decor_counts[key] = decor_counts.get(key, 0) + 1
        summary[f"pool_{pool_name}"] = int(summary[f"pool_{pool_name}"]) + 1

    for pool_name, target in pool_targets.items():
        if len(shortlist) >= shortlist_limit or target <= 0:
            continue
        pool_setups = DATA_POOL_SETUPS.get(pool_name, frozenset())
        candidates = [
            cand
            for cand in dynamic_pool
            if can_add(cand) and any(setup in cand.strategy_fits for setup in pool_setups)
        ]
        candidates.sort(key=_candidate_sort_key, reverse=True)
        added = 0
        for cand in candidates:
            if len(shortlist) >= shortlist_limit or added >= target:
                break
            if not can_add(cand):
                continue
            add_item(cand, pool_name)
            added += 1

    for setup_id in setup_ids:
        if len(shortlist) >= shortlist_limit:
            break
        candidates = [
            cand
            for cand in dynamic_pool
            if cand.symbol not in seen and setup_id in cand.strategy_fits
        ]
        candidates.sort(key=_candidate_sort_key, reverse=True)
        reserved = 0
        for cand in candidates:
            if reserved >= RESERVED_PER_SETUP or len(shortlist) >= shortlist_limit:
                break
            if not can_add(cand):
                summary["decorrelation_skips"] = int(summary["decorrelation_skips"]) + 1
                continue
            pool_name = SETUP_DATA_POOL.get(setup_id, "klines")
            add_item(cand, pool_name)
            summary["strategy_seed"] = int(summary["strategy_seed"]) + 1
            reserved += 1

    for cand in sorted(dynamic_pool, key=_candidate_sort_key, reverse=True):
        if len(shortlist) >= shortlist_limit:
            break
        if not can_add(cand):
            continue
        pool_name = SETUP_DATA_POOL.get(cand.strategy_fits[0], "klines") if cand.strategy_fits else "klines"
        add_item(cand, pool_name)
        summary["fill"] = int(summary["fill"]) + 1

    return summary


def asset_strategy_allowlist(
    symbol: str,
    *,
    settings: BotSettings,
    enabled: set[str],
    heuristic_fits: tuple[str, ...],
) -> tuple[str, ...]:
    """Pinned/priority assets: respect per-asset allow/exclude lists."""
    assets = getattr(settings, "assets", {}) or {}
    asset_cfg = assets.get(symbol.upper()) if isinstance(assets, dict) else None
    excluded = set(getattr(asset_cfg, "excluded_strategies", ()) or ())
    allowed_raw = getattr(asset_cfg, "allowed_strategies", ()) or ()
    allowed = {str(item).strip() for item in allowed_raw if str(item).strip()}

    if allowed:
        return tuple(setup for setup in allowed if setup in enabled and setup not in excluded)

    return tuple(
        setup
        for setup in dict.fromkeys(heuristic_fits)
        if setup in enabled and setup not in excluded
    )
