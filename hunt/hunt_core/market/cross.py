"""Cross-exchange and cross-venue microstructure helpers."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.market.client import aggregate_cross_exchange_walls, depth_snapshot_from_book

LOG = logging.getLogger("hunt_core.market.cross")

SECONDARY_EXCHANGES: tuple[str, ...] = ("bybit", "okx", "bitget")

# Exchange ids hunt knows how to drive via the ccxt factory (linear USDT swap).
SUPPORTED_SECONDARY_EXCHANGES: frozenset[str] = frozenset(
    {"bybit", "okx", "bitget", "gate", "gateio", "kucoinfutures", "mexc", "htx"}
)


def configured_secondary_exchanges() -> tuple[str, ...]:
    """Cross-exchange venue ids from ``HUNT_CROSS_EXCHANGES`` (comma-separated).

    Unset/empty falls back to :data:`SECONDARY_EXCHANGES`. Ids are lowercased,
    de-duplicated (order preserved) and filtered to those the factory supports;
    an unknown id is logged and skipped rather than silently kept.
    """
    raw = os.getenv("HUNT_CROSS_EXCHANGES", "").strip()
    if not raw:
        return SECONDARY_EXCHANGES
    out: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name or name in out:
            continue
        if name not in SUPPORTED_SECONDARY_EXCHANGES:
            LOG.warning("cross_exchange_unsupported_id | id=%s skipped", name)
            continue
        out.append(name)
    if not out:
        LOG.warning(
            "cross_exchange_env_all_unsupported | raw=%s falling_back=%s",
            raw,
            ",".join(SECONDARY_EXCHANGES),
        )
        return SECONDARY_EXCHANGES
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CrossExchangeConfig:
    """Binance = signal universe; secondaries = cross-venue intel only."""

    enabled: bool = True
    exchanges: tuple[str, ...] = SECONDARY_EXCHANGES
    refresh_interval_s: float = 300.0
    max_symbols_per_refresh: int = 24
    ws_enabled: bool = True
    refresh_concurrency: int = 4


def load_cross_exchange_config() -> CrossExchangeConfig:
    def _flag(name: str, *, default: bool) -> bool:
        raw = os.getenv(name, "1" if default else "0").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return default

    return CrossExchangeConfig(
        enabled=_flag("HUNT_MULTI_EXCHANGE", default=True),
        exchanges=configured_secondary_exchanges(),
        ws_enabled=_flag("HUNT_CROSS_WS", default=True),
        refresh_interval_s=float(os.getenv("HUNT_CROSS_REFRESH_S", "300")),
        max_symbols_per_refresh=int(os.getenv("HUNT_CROSS_MAX_SYMBOLS", "24")),
        refresh_concurrency=int(os.getenv("HUNT_CROSS_CONCURRENCY", "4")),
    )


def apply_cross_exchange_env(cfg: CrossExchangeConfig) -> None:
    """Ensure WS plane sees cross-exchange flag before ``HuntCcxtStreams.start()``."""
    if cfg.enabled and cfg.ws_enabled:
        os.environ["HUNT_CROSS_WS"] = "1"
    elif not cfg.ws_enabled:
        os.environ["HUNT_CROSS_WS"] = "0"


def merge_ws_cross_into_snapshot(
    snapshot: dict[str, Any],
    ws_live: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    """Overlay Pro WS funding/mark/index on REST cross snapshot (WS wins when present)."""
    if not ws_live:
        return snapshot
    out = dict(snapshot)
    funding: dict[str, float | None] = dict(out.get("funding") or {})
    mark_price: dict[str, float | None] = dict(out.get("mark_price") or {})
    for ex_name, fields in ws_live.items():
        if not isinstance(fields, dict):
            continue
        fr = fields.get("fundingRate")
        if fr is not None:
            funding[ex_name] = float(fr)
        mp = fields.get("markPrice")
        if mp is not None and float(mp) > 0:
            mark_price[ex_name] = float(mp)
    out["funding"] = funding
    out["mark_price"] = mark_price
    out["ws_overlay"] = True
    rates = [v for v in funding.values() if v is not None]
    if len(rates) >= 2:
        out["funding_spread"] = round(max(rates) - min(rates), 6)
    prices = [v for v in mark_price.values() if v and v > 0]
    if len(prices) >= 2:
        mean_p = sum(prices) / len(prices)
        out["price_divergence_pct"] = round(
            (max(prices) - min(prices)) / mean_p * 100,
            4,
        ) if mean_p > 0 else 0.0
    return out


async def fetch_secondary_ticker_overlay(
    client: Any,
    *,
    cfg: CrossExchangeConfig,
) -> dict[str, dict[str, Any]]:
    """Gather 24h tickers from each configured secondary venue (soft overlay).

    Returns ``{binance_symbol: {exchange: {change_pct, quote_volume}}}``. A venue
    that fails to respond is skipped (degrade gracefully); malformed numeric
    fields are dropped at the client boundary, so values here are already finite.
    """
    if not cfg.enabled or not cfg.exchanges:
        return {}

    async def _one(name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return name, await client.fetch_secondary_tickers(name)
        except Exception as exc:
            LOG.warning("secondary_ticker_overlay_failed | exchange=%s error=%s", name, exc)
            return name, []

    results = await asyncio.gather(*(_one(n) for n in cfg.exchanges))
    overlay: dict[str, dict[str, Any]] = {}
    for name, rows in results:
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            chg = row.get("price_change_percent")
            qvol = row.get("quote_volume")
            if chg is None or qvol is None:
                continue
            overlay.setdefault(sym, {})[name] = {
                "change_pct": float(chg),
                "quote_volume": float(qvol),
            }
    return overlay


def attach_cross_fields(row: dict[str, Any], cx: dict[str, Any]) -> None:
    row["cross_exchange"] = cx
    row["cross_funding_spread"] = cx.get("funding_spread")
    row["cross_funding_consensus"] = cx.get("funding_consensus")
    row["cross_oi_total"] = cx.get("oi_total")
    row["cross_price_divergence_pct"] = cx.get("price_divergence_pct")
    row["cross_listed"] = cx.get("listed")


async def refresh_cross_exchange_cache(
    client: Any,
    symbols: tuple[str, ...] | list[str],
    cache: dict[str, dict[str, Any]],
    *,
    cfg: CrossExchangeConfig,
) -> int:
    """Refresh cross snapshots for Binance watch-universe symbols (capped)."""
    if not cfg.enabled or not symbols:
        return 0
    targets = list(dict.fromkeys(str(s).upper() for s in symbols))[: cfg.max_symbols_per_refresh]
    sem = asyncio.Semaphore(max(1, cfg.refresh_concurrency))
    updated = 0

    async def _one(sym: str) -> None:
        nonlocal updated
        async with sem:
            snap = await client.fetch_cross_exchange_snapshot(sym)
            cache[sym] = snap
            updated += 1

    results = await asyncio.gather(*(_one(s) for s in targets), return_exceptions=True)
    for sym, res in zip(targets, results):
        if isinstance(res, Exception):
            LOG.warning("cross_exchange_refresh_failed | symbol=%s error=%s", sym, res)
    LOG.info(
        "cross_exchange_cache_refreshed | symbols=%s updated=%s exchanges=%s",
        len(targets),
        updated,
        ",".join(cfg.exchanges),
    )
    return updated




LOG = logging.getLogger("hunt_core.market.cross")

_PRIMARY = "binance"


async def fetch_exchange_order_book(
    client: Any,
    symbol: str,
    exchange: str,
    *,
    limit: int = 100,
) -> dict[str, Any] | None:
    """Depth snapshot for one venue."""
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001
    try:
        if exchange == _PRIMARY:
            snap = await client.fetch_order_book_depth_snapshot(bin_sym, limit=limit)
            return snap if snap.get("bid_price") else None
        ccxt_sym = await client._secondary_ccxt_symbol(exchange, bin_sym)  # noqa: SLF001
        if ccxt_sym is None:
            return None
        ex = await client._get_secondary(exchange)  # noqa: SLF001
        if ex is None:
            return None
        ob = await ex.fetch_order_book(ccxt_sym, limit=min(100, max(5, int(limit))))
        bids = [(float(row[0]), float(row[1])) for row in (ob.get("bids") or []) if row]
        asks = [(float(row[0]), float(row[1])) for row in (ob.get("asks") or []) if row]
        if not bids or not asks:
            return None
        snap = depth_snapshot_from_book(bids, asks)
        snap["exchange"] = exchange
        return snap
    except Exception as exc:
        LOG.warning(
            "cross_book_fetch_failed | symbol=%s exchange=%s error=%s",
            bin_sym,
            exchange,
            exc,
        )
        return None


async def fetch_cross_exchange_book_walls(
    client: Any,
    symbol: str,
    *,
    cfg: CrossExchangeConfig | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Ranked walls from Binance + configured secondaries."""
    cfg = cfg or load_cross_exchange_config()
    venues = [_PRIMARY, *cfg.exchanges] if cfg.enabled else [_PRIMARY]
    results = await asyncio.gather(
        *(fetch_exchange_order_book(client, symbol, ex, limit=limit) for ex in venues),
        return_exceptions=True,
    )
    per_ex: dict[str, dict[str, Any]] = {}
    for ex, res in zip(venues, results, strict=True):
        if isinstance(res, dict) and res.get("bid_price"):
            per_ex[ex] = res
    if not per_ex:
        return {"venues": [], "bid_levels": [], "ask_levels": [], "source": "cross_exchange"}
    merged = aggregate_cross_exchange_walls(per_ex)
    merged["per_exchange"] = {
        ex: {
            "bid_levels": snap.get("bid_levels") or [],
            "ask_levels": snap.get("ask_levels") or [],
            "depth_imbalance": snap.get("depth_imbalance"),
        }
        for ex, snap in per_ex.items()
    }
    return merged


async def fetch_cross_exchange_taker_flow(
    client: Any,
    symbol: str,
    *,
    cfg: CrossExchangeConfig | None = None,
    period: str = "5m",
) -> dict[str, Any]:
    """Taker buy/sell ratio per venue + OI-weighted consensus."""
    cfg = cfg or load_cross_exchange_config()
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001

    async def _primary() -> tuple[str, float | None]:
        try:
            val = await client.fetch_taker_ratio(bin_sym, period=period)
            return _PRIMARY, float(val) if val is not None else None
        except Exception:
            return _PRIMARY, None

    async def _secondary(name: str) -> tuple[str, float | None]:
        ccxt_sym = await client._secondary_ccxt_symbol(name, bin_sym)  # noqa: SLF001
        if ccxt_sym is None:
            return name, None
        ex = await client._get_secondary(name)  # noqa: SLF001
        if ex is None:
            return name, None
        try:
            if not getattr(ex, "has", {}).get("fetchLongShortRatio"):
                return name, None
            payload = await ex.fetch_long_short_ratio(ccxt_sym, period=period, limit=1)
            if isinstance(payload, list) and payload:
                item = payload[-1]
                ratio = item.get("longShortRatio") or item.get("ratio")
                return name, float(ratio) if ratio is not None else None
        except Exception as exc:
            LOG.debug("cross_taker_failed | ex=%s sym=%s err=%s", name, bin_sym, exc)
        return name, None

    tasks = [_primary()]
    if cfg.enabled:
        tasks.extend(_secondary(ex) for ex in cfg.exchanges)
    rows = await asyncio.gather(*tasks)
    per_ex = {ex: val for ex, val in rows if val is not None}
    values = list(per_ex.values())
    consensus = round(sum(values) / len(values), 4) if values else None
    return {
        "period": period,
        "per_exchange": per_ex,
        "consensus": consensus,
        "venues": len(per_ex),
        "source": "cross_exchange",
    }


async def fetch_cross_exchange_volume_profile(
    client: Any,
    symbol: str,
    interval: str = "1h",
    *,
    cfg: CrossExchangeConfig | None = None,
    lookback: int = 48,
    buckets: int = 24,
) -> dict[str, Any]:
    """Merge kline volume from Binance + secondaries (volume-weighted POC)."""
    cfg = cfg or load_cross_exchange_config()
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001
    limit = max(lookback + 5, 60)

    async def _klines(exchange: str) -> tuple[str, pl.DataFrame | None, float]:
        try:
            if exchange == _PRIMARY:
                df = await client.fetch_klines(bin_sym, interval, limit=limit)
                if df is None or df.is_empty():
                    return exchange, None, 0.0
                qv = float(df["volume"].tail(lookback).sum() or 0)
                return exchange, df, qv
            ccxt_sym = await client._secondary_ccxt_symbol(exchange, bin_sym)  # noqa: SLF001
            if ccxt_sym is None:
                return exchange, None, 0.0
            sec = await client._get_secondary(exchange)  # noqa: SLF001
            if sec is None:
                return exchange, None, 0.0
            raw = await sec.fetch_ohlcv(ccxt_sym, timeframe=interval, limit=limit)
            if not raw:
                return exchange, None, 0.0
            df = pl.DataFrame(
                raw,
                schema=["open_time", "open", "high", "low", "close", "volume"],
                orient="row",
            )
            qv = float(df["volume"].tail(lookback).sum() or 0)
            return exchange, df, qv
        except Exception as exc:
            LOG.debug("cross_vp_klines_failed | ex=%s sym=%s err=%s", exchange, bin_sym, exc)
            return exchange, None, 0.0

    venues = [_PRIMARY, *cfg.exchanges] if cfg.enabled else [_PRIMARY]
    parts = await asyncio.gather(*(_klines(v) for v in venues))
    weighted_frames: list[pl.DataFrame] = []
    weights: list[float] = []
    per_ex: dict[str, dict[str, float | None]] = {}
    for ex, df, qv in parts:
        if df is None or df.is_empty() or qv <= 0:
            per_ex[ex] = {"poc": None, "weight": 0.0}
            continue
        poc, vah, val = volume_profile_levels(df, lookback=lookback, buckets=buckets)
        per_ex[ex] = {"poc": poc, "vah": vah, "val": val, "weight": qv}
        tail = df.tail(lookback).select(
            [
                pl.col("high"),
                pl.col("low"),
                (pl.col("volume") * pl.lit(qv)).alias("volume"),
            ]
        )
        weighted_frames.append(tail)
        weights.append(qv)

    if not weighted_frames:
        return {"interval": interval, "poc": None, "vah": None, "val": None, "per_exchange": per_ex}

    merged = pl.concat(weighted_frames, how="vertical")
    total_w = sum(weights) or 1.0
    merged = merged.with_columns((pl.col("volume") / pl.lit(total_w)).alias("volume"))
    poc, vah, val = volume_profile_levels(merged, buckets=buckets)
    return {
        "interval": interval,
        "poc": poc,
        "vah": vah,
        "val": val,
        "per_exchange": per_ex,
        "venues": len(weighted_frames),
        "source": "cross_exchange",
    }


async def attach_cross_microstructure(
    client: Any,
    row: dict[str, Any],
    *,
    cfg: CrossExchangeConfig | None = None,
) -> None:
    """Populate row['cross_microstructure'] for pinned / deep probes."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return
    cfg = cfg or load_cross_exchange_config()
    book, taker5, vp1h, vp15 = await asyncio.gather(
        fetch_cross_exchange_book_walls(client, sym, cfg=cfg),
        fetch_cross_exchange_taker_flow(client, sym, cfg=cfg, period="5m"),
        fetch_cross_exchange_volume_profile(client, sym, "1h", cfg=cfg, lookback=48),
        fetch_cross_exchange_volume_profile(client, sym, "15m", cfg=cfg, lookback=96),
    )
    row["cross_microstructure"] = {
        "book_walls": book,
        "taker_flow": taker5,
        "volume_profile_1h": vp1h,
        "volume_profile_15m": vp15,
        "liquidation_note": (
            "Liquidations: Binance forceOrder WS (primary); secondaries have no unified liq feed"
        ),
    }
    if book.get("depth_imbalance") is not None:
        row.setdefault("market", {})["cross_depth_imbalance"] = book["depth_imbalance"]
    if taker5.get("consensus") is not None:
        row.setdefault("market", {})["cross_taker_5m"] = taker5["consensus"]

__all__ = [
    "attach_cross_microstructure",
    "fetch_cross_exchange_book_walls",
    "fetch_cross_exchange_taker_flow",
    "fetch_cross_exchange_volume_profile",
]
