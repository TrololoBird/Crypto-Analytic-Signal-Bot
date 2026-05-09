from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotSettings, _ALL_SETUP_IDS
from .models import SymbolMeta, UniverseSymbol
from .strategy_asset_fit import calculate_strategy_fit_score


UTC = timezone.utc
STABLE_BASE_ASSETS = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "USDS", "DAI"}
SUPPORTED_USDM_CONTRACT_TYPES = {"PERPETUAL", "TRADIFI_PERPETUAL"}
_ASCII_CONTRACT_RE = re.compile(r"^[A-Z0-9]{4,24}$")
_ASCII_ASSET_RE = re.compile(r"^[A-Z0-9]{2,16}$")
_RESERVED_PER_STRATEGY = 2


def _bucket_for_price_change(price_change_pct: float) -> str:
    move = abs(float(price_change_pct))
    if move >= 8.0:
        return "reversal"
    if move >= 2.0:
        return "breakout"
    return "trend"


def _scaled_bucket_targets(total_slots: int) -> dict[str, int]:
    base = {"trend": 12, "breakout": 10, "reversal": 8}
    if total_slots <= 0:
        return {key: 0 for key in base}
    base_total = sum(base.values())
    scaled = {
        key: int(round(total_slots * weight / base_total))
        for key, weight in base.items()
    }
    assigned = sum(scaled.values())
    if assigned < total_slots:
        for key in ("trend", "breakout", "reversal"):
            if assigned >= total_slots:
                break
            scaled[key] += 1
            assigned += 1
    elif assigned > total_slots:
        for key in ("reversal", "breakout", "trend"):
            while assigned > total_slots and scaled[key] > 0:
                scaled[key] -= 1
                assigned -= 1
    return scaled


def _is_supported_contract_symbol(symbol: str, base_asset: str) -> bool:
    if not _ASCII_CONTRACT_RE.fullmatch(symbol):
        return False
    if not _ASCII_ASSET_RE.fullmatch(base_asset):
        return False
    return True


def _bucket_priority(item: UniverseSymbol) -> tuple[float, float, str]:
    move = abs(item.price_change_pct)
    volume_score = math.log10(max(item.quote_volume, 1.0))
    if item.shortlist_bucket == "trend":
        move_fit = max(0.0, 1.0 - min(move, 2.0) / 2.0)
    elif item.shortlist_bucket == "breakout":
        move_fit = max(0.0, 1.0 - min(abs(move - 4.5) / 4.5, 1.0))
    else:
        move_fit = max(0.0, 1.0 - min(abs(move - 11.0) / 12.0, 1.0))
    quality = round(move_fit * 10.0 + volume_score, 6)
    return quality, item.quote_volume, item.symbol


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _crowding_score(row: dict[str, Any]) -> float:
    ratios = []
    for key in (
        "top_account_ls_ratio",
        "top_position_ls_ratio",
        "global_account_ls_ratio",
    ):
        ratio = _safe_float(row.get(key))
        if ratio is not None and ratio > 0.0:
            ratios.append(ratio)
    gap = _safe_float(row.get("top_vs_global_ls_gap"))
    if not ratios and gap is None:
        return 0.55

    penalties: list[float] = []
    for ratio in ratios:
        deviation = abs(ratio - 1.0)
        penalties.append(_clamp((deviation - 0.12) / 0.95))
    if gap is not None:
        penalties.append(_clamp((abs(gap) - 0.05) / 0.45))
    penalty = sum(penalties) / len(penalties) if penalties else 0.0
    return round(_clamp(1.0 - penalty), 6)


def _oi_participation_score(row: dict[str, Any]) -> float:
    oi_change = _safe_float(row.get("oi_change_pct"))
    oi_current = _safe_float(row.get("oi_current"))
    quote_volume = float(row.get("quote_volume") or 0.0)
    last_price = float(row.get("last_price") or 0.0)

    change_score = 0.55
    if oi_change is not None:
        if oi_change >= 12.0:
            change_score = 1.0
        elif oi_change >= 5.0:
            change_score = 0.82
        elif oi_change >= 1.5:
            change_score = 0.65
        elif oi_change <= -8.0:
            change_score = 0.18
        elif oi_change <= -2.0:
            change_score = 0.35
        else:
            change_score = 0.5

    notional_score = 0.55
    if (
        oi_current is not None
        and oi_current > 0.0
        and quote_volume > 0.0
        and last_price > 0.0
    ):
        oi_notional_ratio = (oi_current * last_price) / quote_volume
        notional_score = _clamp(oi_notional_ratio / 1.6)
    return round(change_score * 0.65 + notional_score * 0.35, 6)


def _funding_basis_sanity_score(row: dict[str, Any]) -> float:
    funding_rate = _safe_float(row.get("funding_rate"))
    basis_pct = _safe_float(row.get("basis_pct"))
    if funding_rate is None and basis_pct is None:
        return 0.55

    funding_score = 0.7
    if funding_rate is not None:
        funding_abs = abs(funding_rate)
        if funding_abs <= 0.0004:
            funding_score = 0.9
        elif funding_abs <= 0.0008:
            funding_score = 0.72
        elif funding_abs <= 0.0012:
            funding_score = 0.45
        else:
            funding_score = 0.15

    basis_score = 0.7
    if basis_pct is not None:
        basis_abs = abs(basis_pct)
        if basis_abs <= 0.05:
            basis_score = 0.9
        elif basis_abs <= 0.12:
            basis_score = 0.72
        elif basis_abs <= 0.2:
            basis_score = 0.45
        else:
            basis_score = 0.15
    return round(funding_score * 0.5 + basis_score * 0.5, 6)


def _strategy_fits_for_row(
    row: dict[str, Any],
    *,
    settings: BotSettings,
    liquidity_rank: int,
) -> tuple[str, ...]:
    fits: list[str] = []
    funding_rate = _safe_float(row.get("funding_rate"))
    basis_pct = _safe_float(row.get("basis_pct"))
    oi_change_pct = _safe_float(row.get("oi_change_pct"))
    quote_volume = float(row.get("quote_volume") or 0.0)
    price_change_pct = abs(float(row.get("price_change_pct") or 0.0))
    spread_bps = _safe_float(row.get("spread_bps"))
    crowding = _crowding_score(row)
    symbol = str(row.get("symbol") or "").strip().upper()

    volume_floor = max(float(settings.universe.min_quote_volume_usd), 1.0)
    volume_multiple = quote_volume / volume_floor
    spread_ok = spread_bps is None or spread_bps <= float(
        settings.universe.shortlist_spread_max_bps
    )
    liquid_enough = quote_volume >= max(volume_floor * 3.0, 30_000_000.0)
    top_liquidity = liquidity_rank <= max(int(settings.universe.shortlist_limit), 30)
    trending_move = price_change_pct <= 3.0
    breakout_move = 2.0 <= price_change_pct <= 10.0
    reversal_move = price_change_pct >= 5.0
    oi_rising = oi_change_pct is not None and oi_change_pct >= 1.0
    oi_extreme = oi_change_pct is not None and abs(oi_change_pct) >= 3.0
    crowd_extreme = crowding <= 0.45

    if spread_ok and volume_multiple >= 1.0 and trending_move:
        fits.extend(
            (
                "ema_bounce",
                "structure_pullback",
                "vwap_trend",
                "supertrend_follow",
                "multi_tf_trend",
                "fvg_setup",
                "cvd_divergence",
                "btc_correlation",
                "altcoin_season_index",
            )
        )
    if spread_ok and volume_multiple >= 1.5 and (breakout_move or oi_rising):
        fits.extend(
            (
                "structure_break_retest",
                "squeeze_setup",
                "bb_squeeze",
                "atr_expansion",
                "bos_choch",
                "fvg_setup",
                "order_block",
                "breaker_block",
                "session_killzone",
                "price_velocity",
                "volume_anomaly",
                "keltner_breakout",
                "spread_strategy",
                "depth_imbalance",
                "whale_walls",
                "aggression_shift",
            )
        )
    if spread_ok and liquid_enough and (reversal_move or crowd_extreme or oi_extreme):
        fits.extend(
            (
                "wick_trap_reversal",
                "hidden_divergence",
                "rsi_divergence_bottom",
                "turtle_soup",
                "liquidity_sweep",
                "stop_hunt_detection",
                "wyckoff_spring",
                "liquidation_heatmap",
                "absorption",
                "volume_climax_reversal",
            )
        )
    if (
        (funding_rate is not None and abs(funding_rate) >= 0.0004)
        or (basis_pct is not None and abs(basis_pct) >= 0.08)
        or (oi_change_pct is not None and abs(oi_change_pct) >= 1.5)
    ):
        fits.append("funding_reversal")
        fits.append("ls_ratio_extreme")
        fits.append("oi_divergence")

    if top_liquidity and liquid_enough and spread_ok:
        fits.extend(
            (
                "liquidity_sweep",
                "vwap_trend",
                "keltner_breakout",
                "whale_walls",
                "spread_strategy",
                "depth_imbalance",
            )
        )

    if symbol in set(settings.universe.pinned_symbols):
        fits.extend(_ALL_SETUP_IDS)

    if not fits and spread_ok and quote_volume >= volume_floor:
        fits.extend(
            (
                "structure_pullback",
                "vwap_trend",
                "fvg_setup",
                "cvd_divergence",
                "price_velocity",
                "multi_tf_trend",
                "spread_strategy",
            )
        )

    market_context = {
        "symbol": symbol,
        "base_asset": str(row.get("base_asset") or "").strip().upper(),
        "liquidity_rank": liquidity_rank,
        "quote_volume": quote_volume,
        "price_change_pct": price_change_pct,
        "spread_bps": spread_bps,
        "book_age_seconds": _safe_float(row.get("book_age_seconds")),
        "funding_rate": funding_rate,
        "oi_current": _safe_float(row.get("oi_current")),
        "oi_change_pct": oi_change_pct,
    }
    setups_config = getattr(settings, "setups", None)
    if setups_config is not None and hasattr(setups_config, "enabled_setup_ids"):
        enabled = set(setups_config.enabled_setup_ids())
    else:
        enabled = set(_ALL_SETUP_IDS)
    return tuple(
        setup_id
        for setup_id in dict.fromkeys(fits)
        if setup_id in enabled
        and calculate_strategy_fit_score(
            symbol,
            setup_id,
            market_context,
            settings=settings,
        )
        > 0.0
    )


def _spread_freshness_score(row: dict[str, Any], settings: BotSettings) -> float:
    universe = settings.universe
    max_spread = float(getattr(universe, "shortlist_spread_max_bps", 8.0))
    stale_s = float(getattr(universe, "shortlist_book_stale_seconds", 90.0))
    spread_bps = _safe_float(row.get("spread_bps"))
    book_age = _safe_float(row.get("book_age_seconds"))
    mark_age = _safe_float(row.get("mark_price_age_seconds"))
    ticker_age = _safe_float(row.get("ticker_age_seconds"))

    spread_score = 0.55
    if spread_bps is not None and spread_bps > 0.0:
        spread_score = _clamp(1.0 - (spread_bps / max_spread))

    freshness_values: list[float] = []
    for age in (ticker_age, book_age, mark_age):
        if age is not None:
            freshness_values.append(_clamp(1.0 - (age / stale_s)))
    freshness_score = (
        sum(freshness_values) / len(freshness_values) if freshness_values else 0.55
    )
    return round(spread_score * 0.55 + freshness_score * 0.45, 6)


def _composite_score(
    *,
    row: dict[str, Any],
    settings: BotSettings,
    liquidity_rank: int,
    eligible_count: int,
    min_onboard_ms: int,
) -> tuple[float, tuple[str, ...]]:
    shortlist_bucket = _bucket_for_price_change(
        float(row.get("price_change_pct") or 0.0)
    )
    liquidity_curve = 1.0 - ((liquidity_rank - 1) / max(eligible_count - 1, 1))
    volume_floor = max(
        float(getattr(settings.universe, "min_quote_volume_usd", 0.0)), 1.0
    )
    volume = float(row.get("quote_volume") or 0.0)
    liquidity_depth = _clamp(
        (math.log10(max(volume, 1.0)) - math.log10(volume_floor)) / 2.0 + 0.5
    )
    liquidity_score = round(liquidity_curve * 0.7 + liquidity_depth * 0.3, 6)

    onboard_date_ms = int(row.get("onboard_date_ms") or 0)
    age_score = 0.55
    if onboard_date_ms > 0:
        age_days = max((min_onboard_ms - onboard_date_ms) / 86_400_000.0, 0.0)
        age_score = _clamp(
            age_days / max(float(settings.universe.min_listing_age_days) * 5.0, 30.0)
        )

    move = abs(float(row.get("price_change_pct") or 0.0))
    if shortlist_bucket == "trend":
        bucket_fit = max(0.0, 1.0 - min(move, 2.0) / 2.0)
    elif shortlist_bucket == "breakout":
        bucket_fit = max(0.0, 1.0 - min(abs(move - 4.5) / 4.5, 1.0))
    else:
        bucket_fit = max(0.0, 1.0 - min(abs(move - 11.0) / 12.0, 1.0))

    tradability_score = 1.0
    if (
        row.get("status") != "TRADING"
        or str(row.get("contract_type") or "").upper()
        not in SUPPORTED_USDM_CONTRACT_TYPES
    ):
        tradability_score = 0.0
    elif float(row.get("last_price") or 0.0) <= 0.0:
        tradability_score = 0.0
    else:
        tradability_score = 0.75 + bucket_fit * 0.25

    freshness_score = _spread_freshness_score(row, settings)
    oi_score = _oi_participation_score(row)
    sanity_score = _funding_basis_sanity_score(row)
    crowding_score = _crowding_score(row)

    score = (
        liquidity_score * 0.32
        + age_score * 0.12
        + tradability_score * 0.18
        + freshness_score * 0.14
        + oi_score * 0.10
        + sanity_score * 0.08
        + crowding_score * 0.06
    )

    reasons: list[str] = [f"bucket:{shortlist_bucket}"]
    if liquidity_rank <= 10:
        reasons.append(f"liquidity_rank:{liquidity_rank}")
    if freshness_score >= 0.72:
        reasons.append("spread_freshness_strong")
    if oi_score >= 0.72:
        reasons.append("oi_participation_strong")
    if sanity_score >= 0.72:
        reasons.append("funding_basis_sane")
    if crowding_score <= 0.35:
        reasons.append("crowding_penalty")
    if age_score >= 0.8:
        reasons.append("seasoned_listing")
    return round(score, 6), tuple(reasons[:4])


def build_shortlist(
    symbol_meta: list[SymbolMeta],
    tickers_24h: list[dict[str, float | str]],
    settings: BotSettings,
    *,
    seed_source: str = "rest_full",
) -> tuple[list[UniverseSymbol], dict[str, Any]]:
    meta_map = {row.symbol: row for row in symbol_meta}
    pinned = {symbol for symbol in settings.universe.pinned_symbols}
    min_onboard = datetime.now(UTC) - timedelta(
        days=settings.universe.min_listing_age_days
    )
    min_onboard_ms = int(min_onboard.timestamp() * 1000)
    eligible_rows: list[dict[str, Any]] = []

    for row in tickers_24h:
        symbol = str(row.get("symbol") or "").strip().upper()
        meta = meta_map.get(symbol)
        if meta is None:
            continue
        if not _is_supported_contract_symbol(symbol, meta.base_asset.upper()):
            continue
        if meta.status.upper() != "TRADING":
            continue
        if meta.contract_type.upper() not in SUPPORTED_USDM_CONTRACT_TYPES:
            continue
        if meta.quote_asset.upper() != settings.universe.quote_asset:
            continue
        if meta.base_asset.upper() in STABLE_BASE_ASSETS:
            continue
        quote_volume = float(row.get("quote_volume") or 0.0)
        last_price = float(row.get("last_price") or 0.0)
        price_change_pct = float(row.get("price_change_percent") or 0.0)
        if quote_volume <= 0.0 or last_price <= 0.0:
            continue
        if symbol not in pinned:
            if quote_volume < settings.universe.min_quote_volume_usd:
                continue
            if meta.onboard_date_ms and meta.onboard_date_ms > min_onboard_ms:
                continue
        eligible_rows.append(
            {
                "symbol": symbol,
                "base_asset": meta.base_asset,
                "quote_asset": meta.quote_asset,
                "contract_type": meta.contract_type,
                "status": meta.status,
                "onboard_date_ms": meta.onboard_date_ms,
                "quote_volume": quote_volume,
                "price_change_pct": price_change_pct,
                "last_price": last_price,
                "shortlist_bucket": _bucket_for_price_change(price_change_pct),
                "spread_bps": _safe_float(row.get("spread_bps")),
                "ticker_age_seconds": _safe_float(row.get("ticker_age_seconds")),
                "book_age_seconds": _safe_float(row.get("book_age_seconds")),
                "mark_price_age_seconds": _safe_float(
                    row.get("mark_price_age_seconds")
                ),
                "oi_change_pct": _safe_float(row.get("oi_change_pct")),
                "oi_current": _safe_float(row.get("oi_current")),
                "funding_rate": _safe_float(row.get("funding_rate")),
                "basis_pct": _safe_float(row.get("basis_pct")),
                "top_account_ls_ratio": _safe_float(row.get("top_account_ls_ratio")),
                "top_position_ls_ratio": _safe_float(row.get("top_position_ls_ratio")),
                "global_account_ls_ratio": _safe_float(
                    row.get("global_account_ls_ratio")
                ),
                "top_vs_global_ls_gap": _safe_float(row.get("top_vs_global_ls_gap")),
            }
        )

    eligible_rows.sort(key=lambda item: float(item["quote_volume"]), reverse=True)
    eligible: list[UniverseSymbol] = []
    liquidity_rank = 0
    previous_volume: float | None = None
    for index, row in enumerate(eligible_rows, start=1):
        row_volume = float(row["quote_volume"])
        if previous_volume is None or not math.isclose(
            row_volume, previous_volume, rel_tol=0.0, abs_tol=1e-9
        ):
            liquidity_rank = index
            previous_volume = row_volume
        shortlist_score, reasons = _composite_score(
            row=row,
            settings=settings,
            liquidity_rank=liquidity_rank,
            eligible_count=len(eligible_rows),
            min_onboard_ms=min_onboard_ms,
        )
        eligible.append(
            UniverseSymbol(
                symbol=str(row["symbol"]),
                base_asset=str(row["base_asset"]),
                quote_asset=str(row["quote_asset"]),
                contract_type=str(row["contract_type"]),
                status=str(row["status"]),
                onboard_date_ms=int(row["onboard_date_ms"]),
                quote_volume=float(row["quote_volume"]),
                price_change_pct=float(row["price_change_pct"]),
                last_price=float(row["last_price"]),
                shortlist_bucket=str(row["shortlist_bucket"]),
                shortlist_score=shortlist_score,
                shortlist_reasons=reasons,
                seed_source=seed_source,
                liquidity_rank=liquidity_rank,
                strategy_fits=_strategy_fits_for_row(
                    row,
                    settings=settings,
                    liquidity_rank=liquidity_rank,
                ),
            )
        )

    eligible.sort(
        key=lambda item: (
            item.shortlist_score or 0.0,
            _bucket_priority(item)[0],
            item.quote_volume,
            item.symbol,
        ),
        reverse=True,
    )
    pinned_rows = [row for row in eligible if row.symbol in pinned]
    dynamic_pool = [row for row in eligible if row.symbol not in pinned][
        : settings.universe.dynamic_limit
    ]
    bucket_pool: dict[str, list[UniverseSymbol]] = {
        "trend": [],
        "breakout": [],
        "reversal": [],
    }
    for row in dynamic_pool:
        bucket_pool[row.shortlist_bucket].append(row)
    for bucket in bucket_pool.values():
        bucket.sort(
            key=lambda item: (
                item.shortlist_score or 0.0,
                _bucket_priority(item)[0],
                item.quote_volume,
                item.symbol,
            ),
            reverse=True,
        )

    shortlist: list[UniverseSymbol] = []
    seen: set[str] = set()
    for row in pinned_rows:
        if row.symbol in seen:
            continue
        shortlist.append(row)
        seen.add(row.symbol)

    targets = _scaled_bucket_targets(
        max(settings.universe.shortlist_limit - len(shortlist), 0)
    )
    summary = {
        "mode": seed_source,
        "eligible": len(eligible),
        "dynamic_pool": len(dynamic_pool),
        "pinned": len(pinned_rows),
        "trend": 0,
        "breakout": 0,
        "reversal": 0,
        "fill": 0,
        "strategy_seed": 0,
        "avg_score": round(
            sum((row.shortlist_score or 0.0) for row in shortlist)
            / max(len(shortlist), 1),
            6,
        ),
    }

    for setup_id in _ALL_SETUP_IDS:
        if len(shortlist) >= settings.universe.shortlist_limit:
            break
        candidates = [
            candidate
            for candidate in dynamic_pool
            if candidate.symbol not in seen and setup_id in candidate.strategy_fits
        ]
        candidates.sort(key=lambda item: item.quote_volume, reverse=True)
        for row in candidates[:_RESERVED_PER_STRATEGY]:
            shortlist.append(row)
            seen.add(row.symbol)
            summary["strategy_seed"] += 1
            if len(shortlist) >= settings.universe.shortlist_limit:
                break

    for bucket in ("trend", "breakout", "reversal"):
        for row in bucket_pool[bucket]:
            if (
                len(shortlist) >= settings.universe.shortlist_limit
                or summary[bucket] >= targets[bucket]
            ):
                break
            if row.symbol in seen:
                continue
            if not row.strategy_fits:
                continue
            shortlist.append(row)
            seen.add(row.symbol)
            summary[bucket] += 1

    for row in dynamic_pool:
        if len(shortlist) >= settings.universe.shortlist_limit:
            break
        if row.symbol in seen:
            continue
        if not row.strategy_fits:
            continue
        shortlist.append(row)
        seen.add(row.symbol)
        summary["fill"] += 1

    shortlist.sort(
        key=lambda item: (
            item.symbol not in pinned,
            -(item.shortlist_score or 0.0),
            -_bucket_priority(item)[0],
            -item.quote_volume,
            item.symbol,
        )
    )
    summary["avg_score"] = round(
        sum((row.shortlist_score or 0.0) for row in shortlist) / max(len(shortlist), 1),
        6,
    )
    strategy_counts = {setup_id: 0 for setup_id in _ALL_SETUP_IDS}
    for row in shortlist:
        for setup_id in row.strategy_fits:
            if setup_id in strategy_counts:
                strategy_counts[setup_id] += 1
    summary["strategy_fit_counts"] = {
        key: value for key, value in strategy_counts.items() if value > 0
    }
    return shortlist, summary
