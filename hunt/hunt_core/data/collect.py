"""REST/WS data ingest (P2 — no detect/analysis imports)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

import polars as pl

LOG = logging.getLogger("hunt_core.data.collect")

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.market import HuntCcxtClient, HuntCcxtStreams

def kline_limits(minimums: dict[str, int], symbol: str = "") -> dict[str, int]:
    """Hunt watch pulls deeper history than default bot warmup (max 1500 bars)."""
    limits: dict[str, int] = {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
        "1d": 90,
    }
    if symbol.upper() in PINNED_SYMBOLS:
        limits["1w"] = 52  # ~1 year of weekly bars for MTF structure
    return limits


def _kline_integrity_reject(
    *,
    symbol: str,
    report: Any,
    fetch_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    violations = list(report.violations)
    primary = violations[0] if violations else "data.klines_incomplete"
    return {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "error": primary,
        "no_signal_reason": primary,
        "data_violations": violations[:16],
        "fetch_errors": dict(fetch_errors or {}),
        "data_integrity": {
            "complete": False,
            "violations": violations,
            "details": dict(report.details),
        },
    }



async def safe_fetch(coro: Any, *, context: str = "") -> Any:
    try:
        return await coro
    except DEFENSIVE_EXC as exc:
        LOG.warning(
            "safe_fetch_failed | context=%s error=%s",
            context or type(coro).__name__,
            exc,
        )
        return None



def _book_from_pack(pack: dict[str, Any]) -> dict[str, float | None]:
    depth = pack.get("book_depth")
    if isinstance(depth, dict) and depth.get("bid_price"):
        return depth
    ticker = pack.get("book_ticker")
    return ticker if isinstance(ticker, dict) else {}




def _apply_cross_exchange_flat(row: dict[str, Any]) -> None:
    """Promote nested cross_exchange aggregates to top-level row fields."""
    cx = row.get("cross_exchange")
    if not isinstance(cx, dict):
        return
    row["cross_funding_spread"] = cx.get("funding_spread")
    row["cross_funding_consensus"] = cx.get("funding_consensus")
    row["cross_oi_total"] = cx.get("oi_total")
    row["cross_price_divergence_pct"] = cx.get("price_divergence_pct")


async def _attach_cross_market_fields(
    market: dict[str, Any],
    *,
    client: HuntCcxtClient,
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
) -> None:
    """Set cross_data_source on market: ws when cross WS live, else REST fallback."""
    cross_ws = ws_feed is not None and ws_feed.cross_ws_connected
    if cross_ws:
        market["cross_data_source"] = "ws"
        ws_cross = ws_feed.live_funding_cross(symbol)
        if ws_cross:
            market["cross_funding_secondary"] = {
                ex: fields.get("fundingRate")
                for ex, fields in ws_cross.items()
                if isinstance(fields, dict)
            }
        return

    try:
        snap = await client.fetch_cross_exchange_snapshot(symbol)
    except Exception as exc:
        LOG.warning("cross_rest_fallback_failed | symbol=%s error=%s", symbol, exc)
        market["cross_data_source"] = "unavailable"
        return

    funding = snap.get("funding") if isinstance(snap.get("funding"), dict) else {}
    oi_usd = snap.get("oi_usd") if isinstance(snap.get("oi_usd"), dict) else {}
    secondary_funding = {
        k: v for k, v in funding.items() if k != "binance" and v is not None
    }
    secondary_oi = {k: v for k, v in oi_usd.items() if v is not None}
    if secondary_funding or secondary_oi:
        market["cross_data_source"] = "rest"
        if secondary_funding:
            market["cross_funding_secondary"] = secondary_funding
        if secondary_oi:
            market["cross_oi_usd_secondary"] = secondary_oi
        if snap.get("funding_spread") is not None:
            market["cross_funding_spread"] = snap.get("funding_spread")
        if snap.get("oi_total") is not None:
            market["cross_oi_total"] = snap.get("oi_total")
        if snap.get("funding_consensus") is not None:
            market["cross_funding_consensus"] = snap.get("funding_consensus")
    else:
        market["cross_data_source"] = "unavailable"

def _btc_corr_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 24) -> float | None:
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None

    sym_close = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    btc_close = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    sym_r = sym_close.pct_change().drop_nulls()
    btc_r = btc_close.pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    corr_df = pl.DataFrame({"sym": sym_r.tail(n), "btc": btc_r.tail(n)})
    corr_val = corr_df.select(pl.corr("sym", "btc")).item()
    return round(float(corr_val), 4) if corr_val is not None else None


def _btc_beta_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 48) -> float | None:
    """Rolling OLS beta of symbol vs BTC 1h returns via polars_ols."""
    try:
        import polars as pl
        import polars_ols  # noqa: PLC0415
    except ImportError:
        return None
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None
    sym_r = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    btc_r = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    tmp = pl.DataFrame({"y": sym_r.tail(n), "x": btc_r.tail(n)})
    try:
        result = polars_ols.compute_least_squares(tmp["y"], features=[tmp["x"]], add_intercept=True)
        beta = float(result.get_column("x")[0])
        return round(beta, 4)
    except Exception:
        return None




def _enrich_work_research_frames(prepared: Any) -> None:
    """Attach OLS trend + trading metrics to primary work frames (Phases 11A/11B)."""
    for attr in ("work_15m", "work_1h"):
        work = getattr(prepared, attr, None)
        if work is None or getattr(work, "is_empty", lambda: True)():
            continue
        try:
            enriched = enrich_research_columns(work)
            setattr(prepared, attr, enriched)
        except Exception:
            continue


def _merge_research_tf_fields(out: dict[str, Any], df: Any) -> dict[str, Any]:
    fields = research_snapshot_fields(df)
    if fields:
        out.update(fields)
    return out


def _attach_research_setup_fields(setup: dict[str, Any], *, tf: dict[str, Any], regime: dict[str, Any]) -> None:
    block = tf.get("15m_closed") or tf.get("15m") or {}
    if isinstance(block, dict):
        for key in (
            "trend_slope_20",
            "residual_vol",
            "sharpe_20",
            "current_drawdown",
            "return_entropy_50",
        ):
            if key in block and block[key] is not None:
                setup[key] = block[key]
    if regime.get("return_entropy_50") is not None:
        setup["return_entropy_50"] = regime["return_entropy_50"]
    if regime.get("volume_regime_break"):
        setup["volume_regime_break"] = True


def _apply_rest_enrichments(
    prepared: Any,
    *,
    client: HuntCcxtClient,
    symbol: str,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    funding_info: dict[str, float | int] | None,
    delta: float | None,
) -> None:
    prepared.oi_current = pack.get("oi") or client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = pack.get("oi_chg_1h") or client.get_cached_oi_change(symbol, "1h")
    prepared.ls_ratio = pack.get("ls_1h") or client.get_cached_ls_ratio(symbol, "1h")
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = pack.get(
        "top_ls_1h"
    ) or client.get_cached_top_position_ls_ratio(symbol, "1h")
    prepared.top_trader_position_ratio = prepared.top_position_ls_ratio
    prepared.global_ls_ratio = pack.get("global_ls_1h") or client.get_cached_global_ls_ratio(
        symbol, "1h"
    )
    prepared.global_account_ls_ratio = prepared.global_ls_ratio
    if prepared.ls_ratio is not None and prepared.global_ls_ratio is not None:
        prepared.top_vs_global_ls_gap = float(prepared.ls_ratio) - float(prepared.global_ls_ratio)
    prepared.taker_ratio = pack.get("taker_1h") or client.get_cached_taker_ratio(symbol, "1h")
    prepared.funding_rate = pack.get("funding") or client.get_cached_funding_rate(symbol)
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    funding_z = client.get_cached_funding_rate_zscore(symbol)
    if funding_z is not None:
        prepared.funding_rate_zscore_48h = float(funding_z)
    extreme = client.get_cached_funding_recent_extreme(symbol)
    if extreme is not None:
        prepared.funding_recent_extreme_rate = float(extreme[0])
        prepared.funding_recent_extreme_age_hours = float(extreme[1])
    basis_stats = client.get_cached_basis_stats(symbol, period="5m")
    if basis_stats:
        if basis_stats.get("basis_pct") is not None:
            prepared.basis_pct = float(basis_stats["basis_pct"])
        if basis_stats.get("premium_zscore_5m") is not None:
            prepared.premium_zscore_5m = float(basis_stats["premium_zscore_5m"])
        if basis_stats.get("premium_slope_5m") is not None:
            prepared.premium_slope_5m = float(basis_stats["premium_slope_5m"])
    basis_direct = pack.get("basis_5m")
    if basis_direct is not None and prepared.basis_pct is None:
        prepared.basis_pct = float(basis_direct)
        prepared.mark_index_spread_bps = float(basis_direct) * 100.0
    if premium_row:
        from hunt_core.coercion import finite_float_or_none

        mark = finite_float_or_none(premium_row.get("mark_price"))
        index = finite_float_or_none(premium_row.get("index_price"))
        if mark is not None and mark > 0:
            prepared.mark_price = mark
        if prepared.funding_rate is None:
            funding = finite_float_or_none(premium_row.get("funding_rate"))
            if funding is not None:
                prepared.funding_rate = funding
        if mark is not None and index is not None and mark > 0 and index > 0:
            basis = (mark / index - 1.0) * 100.0
            prepared.basis_pct = basis
            prepared.mark_index_spread_bps = basis * 100.0
        if premium_row.get("estimated_settle_price"):
            prepared.estimated_settle_price = float(premium_row["estimated_settle_price"])
        if premium_row.get("interest_rate") is not None:
            prepared.interest_rate = float(premium_row["interest_rate"])
        if premium_row.get("next_funding_time_ms"):
            prepared.next_funding_time_ms = int(premium_row["next_funding_time_ms"])
    if funding_info:
        if funding_info.get("funding_rate_cap") is not None:
            prepared.funding_rate_cap = float(funding_info["funding_rate_cap"])
        if funding_info.get("funding_rate_floor") is not None:
            prepared.funding_rate_floor = float(funding_info["funding_rate_floor"])
        if funding_info.get("funding_interval_hours") is not None:
            prepared.funding_interval_hours = int(funding_info["funding_interval_hours"])
    prepared.depth_imbalance = depth_imbalance_from_book(
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.microprice_bias = microprice_bias_from_book(
        bid=book.get("bid_price"),
        ask=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.depth_imbalance_source = "rest_depth" if pack.get("book_depth") else "rest_ticker"
    prepared.microprice_bias_source = prepared.depth_imbalance_source
    agg = pack.get("agg_trades")
    if agg is not None:
        prepared.agg_trade_delta_30s = getattr(agg, "delta_ratio", None)
        prepared.orderflow_source = "agg_trade_rest"
    prepared.data_source_mix = "futures_rest_full"


def _overlay_ws_market(prepared: Any, ws_snap: dict[str, Any] | None) -> None:
    """Prefer live WS orderflow + mark/ap between REST polls (reports A7/A8)."""
    if not ws_snap:
        return
    ws_delta = ws_snap.get("agg_trade_delta_30s")
    if ws_delta is not None:
        prepared.agg_trade_delta_30s = float(ws_delta)
        prepared.orderflow_source = str(ws_snap.get("agg_trade_source") or "ws_nq")
    if ws_snap.get("funding_live") is not None:
        prepared.funding_rate = float(ws_snap["funding_live"])
    if ws_snap.get("mark_live") is not None:
        prepared.mark_price = float(ws_snap["mark_live"])
    if ws_snap.get("basis_bps_live") is not None:
        bps = float(ws_snap["basis_bps_live"])
        prepared.basis_pct = bps / 100.0
        prepared.mark_index_spread_bps = bps
    if ws_snap.get("basis_ap_bps") is not None:
        ap_bps = float(ws_snap["basis_ap_bps"])
        prepared.basis_pct = ap_bps / 100.0
        prepared.mark_index_spread_bps = ap_bps
    live_di = ws_snap.get("live_depth_imbalance")
    if live_di is not None and ws_snap.get("ws_connected"):
        prepared.depth_imbalance = float(live_di)
        prepared.depth_imbalance_source = "ws_book"
    live_mp = ws_snap.get("live_microprice_bias")
    if live_mp is not None and ws_snap.get("ws_connected"):
        prepared.microprice_bias = float(live_mp)
        prepared.microprice_bias_source = "ws_book"


async def fetch_rest_pack(
    client: HuntCcxtClient,
    symbol: str,
    *,
    tier: SnapshotTier = "full",
    ws_feed: HuntCcxtStreams | None = None,
) -> dict[str, Any]:
    """Fetch public REST enrichment; fast tier keeps dump-onset fields."""
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    specs = rest_pack_specs(
        client,
        symbol,
        tier=tier,
        ws_orderflow_fresh=ws_orderflow_fresh(ws_snap),
    )
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    pack: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        pack[name] = None if isinstance(res, BaseException) else res
    depth = pack.get("book_depth")
    if not isinstance(depth, dict) or not depth.get("bid_price"):
        from hunt_core.data.collect import safe_fetch

        pack["book_ticker"] = await safe_fetch(client._fetch_book_ticker_rest_detail(symbol))
    return pack


_fetch_rest_pack = fetch_rest_pack

# --- merged from data/rest_tiers.py ---



SnapshotTier = Literal["full", "fast"]

PREMIUM_BATCH_TTL_S = 30.0
FUNDING_BATCH_TTL_S = 30.0
EXCHANGE_INFO_TTL_S = 3600.0
BTC_1H_TTL_S = 900.0

_FAST_FRESH_KLINES = frozenset({"1m", "5m"})
_FAST_CACHE_KLINES = ("15m", "1h", "4h", "1d")
FULL_KLINE_ORDER = ("1m", "5m", "15m", "1h", "4h", "1d")


def _fetch_error_label(exc: BaseException) -> str:
    return f"fetch_failed:{type(exc).__name__}"


async def resolve_kline_map(
    client: HuntCcxtClient,
    symbol: str,
    limits: dict[str, int],
    *,
    tier: SnapshotTier,
    safe_fetch: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch klines with tier-aware budget (fast = fresh 1m/5m + cached HTF).

    Returns (kline_map, fetch_errors) — never silent None without a reason key.
    """
    kline_map: dict[str, Any] = {}
    fetch_errors: dict[str, str] = {}
    if tier == "full":
        tasks = {}
        for name in FULL_KLINE_ORDER:
            if name not in limits:
                continue
            if name in _FAST_FRESH_KLINES:
                tasks[name] = client.fetch_klines(symbol, name, limit=limits[name])
            else:
                tasks[name] = client.fetch_klines_cached(symbol, name, limit=limits[name])
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks.keys(), results, strict=True):
            if isinstance(res, BaseException):
                kline_map[name] = None
                fetch_errors[name] = _fetch_error_label(res)
            else:
                kline_map[name] = res
        return kline_map, fetch_errors

    for name in _FAST_FRESH_KLINES:
        if name not in limits:
            continue
        # Fast tier bypasses kline cache (5m TTL up to 300s) — delivery must be fresh.
        res = await safe_fetch(
            client.fetch_klines(symbol, name, limit=limits[name]),
            context=f"klines.{name}",
        )
        kline_map[name] = res
        if res is None:
            fetch_errors[name] = "fetch_failed"

    for name in _FAST_CACHE_KLINES:
        if name not in limits:
            continue
        cached = client.get_cached_klines(symbol, name, limit=limits[name])
        if cached is not None and not cached.is_empty():
            kline_map[name] = cached
            continue
        res = await safe_fetch(
            client.fetch_klines_cached(symbol, name, limit=limits[name]),
            context=f"klines.{name}",
        )
        kline_map[name] = res
        if res is None:
            fetch_errors[name] = "fetch_failed"
    return kline_map, fetch_errors


def rest_pack_specs(
    client: HuntCcxtClient,
    symbol: str,
    *,
    tier: SnapshotTier,
    ws_orderflow_fresh: bool,
) -> list[tuple[str, Any]]:
    """Per-symbol REST pack — fast tier keeps dump-onset fields only."""
    critical: list[tuple[str, Any]] = [
        ("oi", client.fetch_open_interest(symbol)),
        ("oi_chg_5m", client.fetch_open_interest_change(symbol, period="5m")),
        ("top_ls_5m", client.fetch_top_position_ls_ratio(symbol, period="5m")),
        ("global_ls_5m", client.fetch_global_ls_ratio(symbol, period="5m")),
        ("taker_5m", client.fetch_taker_ratio(symbol, period="5m")),
        ("book_depth", client.fetch_order_book_depth_snapshot(symbol, limit=100)),
    ]
    if tier == "fast":
        if not ws_orderflow_fresh:
            critical.append(("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)))
        return critical

    return critical + [
        ("oi_chg_1h", client.fetch_open_interest_change(symbol, period="1h")),
        ("ls_5m", client.fetch_long_short_ratio(symbol, period="5m")),
        ("ls_1h", client.fetch_long_short_ratio(symbol, period="1h")),
        ("top_ls_1h", client.fetch_top_position_ls_ratio(symbol, period="1h")),
        ("global_ls_1h", client.fetch_global_ls_ratio(symbol, period="1h")),
        ("taker_15m", client.fetch_taker_ratio(symbol, period="15m")),
        ("taker_1h", client.fetch_taker_ratio(symbol, period="1h")),
        ("funding", client.fetch_funding_rate(symbol)),
        ("funding_hist", client.fetch_funding_rate_history(symbol, limit=16)),
        ("basis_5m", client.fetch_basis(symbol, period="5m")),
        ("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)),
        ("oi_series", client.fetch_open_interest_series(symbol, period="5m", limit=48)),
        ("gls_series", client.fetch_global_ls_series(symbol, period="5m", limit=48)),
    ]


def ws_orderflow_fresh(ws_snap: dict[str, Any] | None, *, max_age_s: float = 45.0) -> bool:
    if not ws_snap:
        return False
    age = ws_snap.get("ws_last_msg_age_s")
    if age is None:
        return ws_snap.get("ws_connected") is True
    try:
        return float(age) <= max_age_s and ws_snap.get("agg_trade_delta_60s") is not None
    except (TypeError, ValueError):
        return False


def sort_symbols_for_tick(
    symbols: tuple[str, ...],
    *,
    ignition_by_sym: dict[str, Any] | None,
    last_bias: dict[str, str],
) -> tuple[str, ...]:
    """Ignited / short-bias symbols first."""

    def _rank(sym: str) -> tuple[int, str]:
        score = 0
        if ignition_by_sym and sym in ignition_by_sym:
            score += 200
        bias = last_bias.get(sym, "")
        if bias == "short":
            score += 80
        elif bias == "both":
            score += 40
        if sym in {"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"}:
            score += 10
        return (-score, sym)

    return tuple(sorted(symbols, key=_rank))


class TickBatchCache:
    """Loop-scoped cache for all-symbol REST batch fetches."""

    __slots__ = (
        "premium_all",
        "funding_info_all",
        "exchange_by_sym",
        "btc_work_1h",
        "premium_at",
        "funding_at",
        "exchange_at",
        "btc_at",
    )

    def __init__(self) -> None:
        self.premium_all: dict[str, dict[str, float]] = {}
        self.funding_info_all: dict[str, dict[str, float | int]] = {}
        self.exchange_by_sym: dict[str, Any] = {}
        self.btc_work_1h: Any | None = None
        self.premium_at = 0.0
        self.funding_at = 0.0
        self.exchange_at = 0.0
        self.btc_at = 0.0

    def _fresh(self, at: float, ttl: float) -> bool:
        return at > 0 and (time.monotonic() - at) < ttl


async def refresh_tick_batch_cache(
    cache: TickBatchCache,
    client: HuntCcxtClient,
    *,
    safe_fetch: Any,
    prepare_frame: Any,
    need_btc: bool,
    tier: SnapshotTier,
) -> None:
    """Refresh shared batch data — fast tier reuses fresh batch snapshots."""
    now = time.monotonic()
    if tier == "full" or not cache._fresh(cache.premium_at, PREMIUM_BATCH_TTL_S):
        cache.premium_all = await safe_fetch(client.fetch_premium_index_all()) or {}
        cache.premium_at = now
    if tier == "full" or not cache._fresh(cache.funding_at, FUNDING_BATCH_TTL_S):
        cache.funding_info_all = await safe_fetch(client.fetch_funding_info_all()) or {}
        cache.funding_at = now
    if not cache._fresh(cache.exchange_at, EXCHANGE_INFO_TTL_S):
        exchange_list = await safe_fetch(client.fetch_exchange_symbols()) or []
        cache.exchange_by_sym = {r.symbol: r for r in exchange_list}
        cache.exchange_at = now
    if need_btc and (tier == "full" or not cache._fresh(cache.btc_at, BTC_1H_TTL_S)):
        btc_df = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1h", limit=500))
        if btc_df is not None and not btc_df.is_empty():
            cache.btc_work_1h = prepare_frame(btc_df)
        cache.btc_at = now



_fetch_rest_pack = fetch_rest_pack

__all__ = [
    "SnapshotTier",
    "TickBatchCache",
    "safe_fetch",
    "kline_limits",
    "fetch_rest_pack",
    "_fetch_rest_pack",
    "resolve_kline_map",
    "rest_pack_specs",
    "ws_orderflow_fresh",
    "sort_symbols_for_tick",
    "refresh_tick_batch_cache",
    "_book_from_pack",
    "_apply_rest_enrichments",
    "_overlay_ws_market",
    "_kline_integrity_reject",
]
