from __future__ import annotations

import asyncio
import contextlib
import html
import itertools
import logging
import math
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from bot.market.data import BinanceFuturesMarketData
from bot.regime.composite_regime import build_minimal_regime_frame_4h
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.domain.schemas import UniverseSymbol
    from bot.regime.market import MarketRegimeResult
    from bot.runtime.bot import SignalBot


LOG = logging.getLogger("bot.runtime.bot")


class MarketContextUpdater:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot
        self._last_regime: str | None = None
        self._last_market_state_alert_key: str | None = None
        self._last_market_state_alert_at: float = 0.0
        self._last_market_state_html: str | None = None
        self._last_display_snapshot: dict[str, Any] | None = None

    async def market_regime_periodic(self) -> None:
        await asyncio.sleep(10)
        while not self._bot._shutdown.is_set():
            try:
                async with self._bot._shortlist_lock:
                    shortlist = list(self._bot._shortlist)
                if shortlist:
                    await self.update_memory_market_context(shortlist)
                    LOG.debug("market regime periodic update completed")
            except DEFENSIVE_EXC as exc:
                LOG.debug("market regime periodic update failed: %s", exc)
            try:
                await asyncio.wait_for(self._bot._shutdown.wait(), timeout=60)
            except TimeoutError:
                continue

    async def public_intelligence_periodic(self) -> None:
        await asyncio.sleep(45)
        while not self._bot._shutdown.is_set():
            try:
                async with self._bot._shortlist_lock:
                    shortlist = list(self._bot._shortlist)
                if shortlist and self._bot.intelligence is not None:
                    snapshot = await self._bot.intelligence.collect(
                        [item.symbol for item in shortlist]
                    )
                    await self.update_memory_market_context(shortlist)
                    await self.apply_public_guardrails(snapshot)
                    LOG.info(
                        "public intelligence updated | barrier_long=%s barrier_short=%s macro=%s",
                        cast("dict[str, Any]", snapshot.get("barrier") or {}).get(
                            "long_barrier_triggered"
                        ),
                        cast("dict[str, Any]", snapshot.get("barrier") or {}).get(
                            "short_barrier_triggered"
                        ),
                        cast("dict[str, Any]", snapshot.get("macro") or {}).get("risk_mode"),
                    )
            except DEFENSIVE_EXC:
                LOG.exception("public intelligence update failed")
            try:
                await asyncio.wait_for(
                    self._bot._shutdown.wait(),
                    timeout=max(
                        60,
                        int(self._bot.settings.intelligence.refresh_interval_seconds),
                    ),
                )
            except TimeoutError:
                continue

    async def apply_public_guardrails(self, snapshot: dict[str, Any]) -> None:
        if self._bot.intelligence is None:
            return
        open_rows = await self._bot._modern_repo.get_active_signals(include_closed=False)
        if not open_rows:
            return

        barrier = cast("dict[str, Any]", snapshot.get("barrier") or {})
        barrier_events = []
        closed_tracking_ids: set[str] = set()

        if bool(barrier.get("long_barrier_triggered")):
            long_ids = [
                str(row["tracking_id"])
                for row in open_rows
                if str(row.get("direction") or "").lower() == "long"
            ]
            if long_ids:
                note = (
                    f"tracked_signal_hard_barrier_long {barrier.get('strongest_symbol')} "
                    f"{barrier.get('strongest_move_pct')}pct/{barrier.get('window_minutes')}m"
                )
                barrier_events.extend(
                    await self._bot.tracker.force_close_tracking_ids(
                        long_ids,
                        reason="emergency_exit",
                        occurred_at=datetime.now(UTC),
                        note=note,
                    )
                )
                closed_tracking_ids.update(long_ids)

        if bool(barrier.get("short_barrier_triggered")):
            short_ids = [
                str(row["tracking_id"])
                for row in open_rows
                if str(row.get("direction") or "").lower() == "short"
            ]
            if short_ids:
                note = (
                    f"tracked_signal_hard_barrier_short {barrier.get('strongest_symbol')} "
                    f"{barrier.get('strongest_move_pct')}pct/{barrier.get('window_minutes')}m"
                )
                barrier_events.extend(
                    await self._bot.tracker.force_close_tracking_ids(
                        short_ids,
                        reason="emergency_exit",
                        occurred_at=datetime.now(UTC),
                        note=note,
                    )
                )
                closed_tracking_ids.update(short_ids)

        smart_exit_events = []
        if self._bot.settings.intelligence.smart_exit_enabled:
            for row in open_rows:
                tracking_id = str(row.get("tracking_id") or "")
                if not tracking_id or tracking_id in closed_tracking_ids:
                    continue
                if not row.get("activated_at"):
                    continue
                symbol = str(row.get("symbol") or "")
                direction = str(row.get("direction") or "")
                smart_exit = await self._bot.intelligence.evaluate_smart_exit(symbol, direction)
                if not bool(smart_exit.get("triggered")):
                    continue
                smart_exit_events.extend(
                    await self._bot.tracker.force_close_tracking_ids(
                        [tracking_id],
                        reason="smart_exit",
                        occurred_at=datetime.now(UTC),
                        note=";".join(cast("list[str]", smart_exit.get("reasons") or [])[:6]),
                    )
                )

        combined_events = barrier_events + smart_exit_events
        if combined_events:
            await self._bot._deliver_tracking(combined_events)

    async def update_memory_market_context(self, shortlist: list[UniverseSymbol]) -> None:
        try:
            if not isinstance(self._bot.client, BinanceFuturesMarketData):
                return
            high_funding: list[str] = []
            low_funding: list[str] = []
            extreme_threshold = 0.0005
            funding_rates: dict[str, float] = {}

            for item in shortlist:
                bc = getattr(self._bot.client, "_binance_client", self._bot.client)
                cached = bc._funding_rate_cache.get(item.symbol)
                if cached is None:
                    continue
                _, fr = cached
                funding_rates[item.symbol] = fr
                if fr >= extreme_threshold:
                    high_funding.append(item.symbol)
                elif fr <= -extreme_threshold:
                    low_funding.append(item.symbol)

            configured_benchmarks = tuple(
                dict.fromkeys(
                    [
                        *getattr(
                            self._bot.settings.intelligence,
                            "benchmark_symbols",
                            ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                        ),
                        "BTCUSDT",
                        "ETHUSDT",
                        "SOLUSDT",
                        "XAUUSDT",
                        "XAGUSDT",
                        "PAXGUSDT",
                    ]
                )
            )
            benchmark_biases: dict[str, str] = dict.fromkeys(configured_benchmarks, "neutral")
            if self._bot._ws_manager is not None:
                for sym in configured_benchmarks:
                    benchmark_biases[sym] = self.compute_price_bias(sym)
            btc_bias = benchmark_biases.get("BTCUSDT", "neutral")
            eth_bias = benchmark_biases.get("ETHUSDT", "neutral")

            benchmark_context: dict[str, dict[str, Any]] = {}
            for sym in configured_benchmarks:
                bias = benchmark_biases.get(sym, "neutral")
                payload: dict[str, Any] = {"bias": bias}
                payload["pct_1h"] = self._cached_kline_change_pct(sym, "1h")
                payload["pct_4h"] = self._cached_kline_change_pct(sym, "4h")
                payload["oi_change_pct"] = self._bot.client.get_cached_oi_change(sym, period="1h")
                payload["basis_pct"] = self._bot.client.get_cached_basis(sym, period="1h")
                basis_stats = self._bot.client.get_cached_basis_stats(sym, period="5m")
                if basis_stats is not None:
                    payload["premium_slope_5m"] = basis_stats.get("premium_slope_5m")
                    payload["premium_zscore_5m"] = basis_stats.get("premium_zscore_5m")
                benchmark_context[sym] = payload

            await self._attach_regime_frame_4h(benchmark_context)

            ticker_data: list[dict[str, Any]] = []
            all_tickers = await self._bot.client.fetch_ticker_24h()
            ticker_dict = {t.get("symbol"): t for t in all_tickers if isinstance(t, dict)}
            for sym in configured_benchmarks:
                ticker = ticker_dict.get(sym)
                if ticker is None:
                    continue
                try:
                    benchmark_context.setdefault(sym, {})["pct_24h"] = (
                        float(ticker.get("price_change_percent") or 0.0) / 100.0
                    )
                except (TypeError, ValueError):
                    benchmark_context.setdefault(sym, {})["pct_24h"] = None
            for item in shortlist:
                ticker = ticker_dict.get(item.symbol)
                if ticker:
                    ticker_data.append(ticker)

            regime_result = self._bot.market_regime.analyze(
                ticker_data,
                funding_rates,
                benchmark_context=benchmark_context,
            )
            previous_regime = self._last_regime
            if previous_regime != regime_result.regime:
                self._bot.telemetry.append_jsonl(
                    "regime_transitions.jsonl",
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "previous_regime": previous_regime,
                        "new_regime": regime_result.regime,
                        "strength": float(regime_result.strength),
                        "confidence": float(getattr(regime_result, "confidence", 0.0) or 0.0),
                        "detector": str(
                            getattr(
                                self._bot.settings.intelligence,
                                "regime_detector",
                                "legacy",
                            )
                        ),
                    },
                )
                self._last_regime = regime_result.regime
            intelligence_snapshot = (
                self._bot.intelligence.latest_snapshot
                if self._bot.intelligence is not None
                else None
            )
            macro_risk_mode = self._macro_proxy_mode(regime_result)
            if intelligence_snapshot:
                macro_snapshot = cast("dict[str, Any]", intelligence_snapshot.get("macro") or {})
                snapshot_mode = str(macro_snapshot.get("risk_mode") or "").strip()
                if snapshot_mode and not snapshot_mode.startswith("disabled_"):
                    macro_risk_mode = snapshot_mode
            stats: dict[str, Any] = {}
            try:
                stats = await asyncio.wait_for(
                    self._bot._modern_repo.get_tracking_stats(),
                    timeout=1.0,
                )
            except DEFENSIVE_EXC as exc:
                LOG.debug("market-state tracking stats unavailable: %s", exc)
            html_text, display_snapshot = await self._build_market_state_text(
                regime=regime_result,
                macro_risk_mode=macro_risk_mode,
                ticker_rows=all_tickers,
                stats=stats,
            )
            self._last_market_state_html = html_text
            self._last_display_snapshot = display_snapshot
            await self._bot._modern_repo.update_market_context(
                btc_bias,
                eth_bias,
                high_funding,
                low_funding,
                market_regime=regime_result.regime,
                market_regime_confirmed=True,
                macro_risk_mode=macro_risk_mode,
                altcoin_season_index=float(
                    getattr(regime_result, "altcoin_season_index", 50.0) or 50.0
                ),
                btc_phase=str(getattr(regime_result, "btc_phase", "sideways") or "sideways"),
                benchmark_context={
                    **benchmark_context,
                    "_meta": {
                        "regime_cache_age_seconds": round(
                            self._bot.market_regime.cache_age_seconds,
                            3,
                        ),
                    },
                },
                intelligence_snapshot=intelligence_snapshot,
                telegram_html=html_text,
                display_snapshot=display_snapshot,
            )
            LOG.info(
                "market regime updated | regime=%s strength=%.2f btc=%s eth=%s",
                regime_result.regime,
                regime_result.strength,
                regime_result.btc_bias,
                regime_result.eth_bias,
            )
            await self._maybe_send_market_state_update(
                regime_result,
                benchmark_context=benchmark_context,
                macro_risk_mode=macro_risk_mode,
                previous_regime=previous_regime,
                ticker_rows=all_tickers,
                shortlist=shortlist,
            )

        except DEFENSIVE_EXC:
            LOG.exception("memory market context update failed")

    def _cached_kline_closes(self, symbol: str, interval: str) -> list[float]:
        client = self._bot.client
        cache = getattr(client, "_klines_cache", None)
        if not isinstance(cache, dict):
            return []
        best_height = 0
        best_closes: list[float] = []
        for key, cached in cache.items():
            if not isinstance(key, tuple) or len(key) < 2:
                continue
            if key[0] != symbol or key[1] != interval:
                continue
            try:
                _, frame = cached
                if frame is None or frame.is_empty() or "close" not in frame.columns:
                    continue
                if frame.height >= best_height:
                    best_height = int(frame.height)
                    best_closes = [
                        self._safe_float(value)
                        for value in frame["close"].to_list()
                        if self._safe_float(value) > 0.0
                    ]
            except (TypeError, ValueError, AttributeError):
                continue
        return best_closes

    async def _attach_regime_frame_4h(self, benchmark_context: dict[str, dict[str, Any]]) -> None:
        closes = self._cached_kline_closes("BTCUSDT", "4h")
        if len(closes) < 3:
            closes = await self._fetch_close_values("BTCUSDT", "4h", limit=120)
        frame = build_minimal_regime_frame_4h(closes)
        if frame is not None and not frame.is_empty():
            # JSON-safe for SQLite/telemetry; composite_regime accepts dict or DataFrame.
            benchmark_context.setdefault("BTCUSDT", {})["regime_frame_4h"] = {
                col: frame[col].to_list() for col in frame.columns
            }

    def _cached_kline_change_pct(self, symbol: str, interval: str) -> float | None:
        client = self._bot.client
        cache = getattr(client, "_klines_cache", None)
        if not isinstance(cache, dict):
            return None
        best_height = 0
        best_value: float | None = None
        for key, cached in cache.items():
            if not isinstance(key, tuple) or len(key) < 2:
                continue
            if key[0] != symbol or key[1] != interval:
                continue
            try:
                _, frame = cached
                if frame is None or frame.height < 2 or "close" not in frame.columns:
                    continue
                prev_close = float(frame.item(-2, "close") or 0.0)
                last_close = float(frame.item(-1, "close") or 0.0)
                if prev_close <= 0.0 or last_close <= 0.0:
                    continue
                if frame.height >= best_height:
                    best_height = int(frame.height)
                    best_value = (last_close - prev_close) / prev_close
            except (IndexError, TypeError, ValueError, AttributeError):
                continue
        return best_value

    @staticmethod
    def _fmt_pct(value: object) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "+0.00%"
        return f"{numeric * 100:+.2f}%"

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        return numeric if math.isfinite(numeric) else default

    @staticmethod
    def _ticker_symbol(row: dict[str, Any]) -> str:
        return str(row.get("symbol") or "").strip().upper()

    @classmethod
    def _ticker_change_pct(cls, row: dict[str, Any] | None) -> float:
        """Binance ``price_change_percent`` as a display percent (e.g. -5.8 for -5.8%)."""
        return cls._safe_float((row or {}).get("price_change_percent"), 0.0)

    @classmethod
    def _ticker_change_fraction(cls, row: dict[str, Any] | None) -> float:
        return cls._ticker_change_pct(row) / 100.0

    @classmethod
    def _ticker_quote_volume(cls, row: dict[str, Any] | None) -> float:
        return max(0.0, cls._safe_float((row or {}).get("quote_volume"), 0.0))

    @staticmethod
    def _fmt_signed_pct_value(value: float, *, digits: int = 1) -> str:
        """Format a display percent (Binance ticker scale, not a 0–1 fraction)."""
        return f"{value:+.{digits}f}%"

    @staticmethod
    def _fmt_unsigned_pct_value(value: float, *, digits: int = 1) -> str:
        return f"{value:.{digits}f}%"

    @staticmethod
    def _macro_proxy_mode(regime: MarketRegimeResult) -> str:
        if regime.risk_on_off == "risk_off" or regime.regime == "bear":
            return "risk_off_binance_proxy"
        if regime.risk_on_off == "risk_on" or regime.regime == "bull":
            return "risk_on_binance_proxy"
        return "neutral_binance_proxy"

    @staticmethod
    def _risk_label(mode: str) -> str:
        normalized = str(mode or "").lower()
        if "risk_off" in normalized:
            return "risk-off"
        if "risk_on" in normalized:
            return "risk-on"
        return "neutral"

    @staticmethod
    def _trend_bias_ru(change_pct: float, *, strong_threshold: float) -> tuple[str, str]:
        if change_pct <= -strong_threshold:
            return "нисходящий уклон", "импульс вниз"
        if change_pct >= strong_threshold:
            return "восходящий уклон", "импульс вверх"
        if change_pct < 0:
            return "легкий нисходящий уклон", "импульс слабый"
        if change_pct > 0:
            return "легкий восходящий уклон", "импульс слабый"
        return "боковой режим", "импульс отсутствует"

    @classmethod
    def _fear_greed_proxy(
        cls,
        *,
        breadth_share: float,
        btc_24h_pct: float,
        regime: MarketRegimeResult,
        funding_sentiment: str,
    ) -> tuple[int, str]:
        btc_frac = btc_24h_pct / 100.0 if abs(btc_24h_pct) > 1.0 else float(btc_24h_pct)
        score = 50.0
        score += max(-30.0, min(30.0, btc_frac * 500.0))
        score += max(-25.0, min(25.0, (breadth_share - 0.50) * 80.0))
        score += max(-15.0, min(15.0, (float(regime.altcoin_season_index) - 50.0) * 0.35))
        if regime.regime == "bear":
            score -= 12.0
        elif regime.regime == "bull":
            score += 12.0
        if funding_sentiment == "long_heavy" and regime.regime in {"bear", "volatile"}:
            score -= 8.0
        elif funding_sentiment == "short_heavy" and regime.regime in {"bull", "volatile"}:
            score += 5.0
        value = int(max(0.0, min(100.0, round(score))))
        if value <= 24:
            label = "Extreme Fear"
        elif value <= 44:
            label = "Fear"
        elif value <= 55:
            label = "Neutral"
        elif value <= 75:
            label = "Greed"
        else:
            label = "Extreme Greed"
        return value, label

    @classmethod
    def _intraday_vs_24h_note(
        cls,
        *,
        btc_24h_pct: float,
        tf_1h: str,
        tf_15m: str,
    ) -> str | None:
        """Clarify short-TF bounce inside a weak 24h down day (common operator confusion)."""
        if btc_24h_pct > -1.5:
            return None
        up_markers = ("восходящий уклон", "импульс вверх")
        short_term_up = any(marker in tf_15m for marker in up_markers) or any(
            marker in tf_1h for marker in up_markers
        )
        if not short_term_up:
            return None
        return (
            "Краткосрок (1h/15m) отскакивает внутри слабого 24h/4h снижения; "
            "суточный risk-off не снимается — long только после подтверждения на старших ТФ."
        )

    def _liquid_ticker_rows(
        self, ticker_rows: list[dict[str, Any]], *, limit: int = 80
    ) -> list[dict[str, Any]]:
        min_volume = float(getattr(self._bot.settings.universe, "min_quote_volume_usd", 0.0) or 0.0)
        rows = [
            row
            for row in ticker_rows
            if self._ticker_symbol(row).endswith("USDT")
            and self._ticker_quote_volume(row) >= min_volume
        ]
        rows.sort(key=self._ticker_quote_volume, reverse=True)
        return rows[: max(1, int(limit))]

    async def _fetch_close_values(self, symbol: str, interval: str, *, limit: int) -> list[float]:
        try:
            frame = await asyncio.wait_for(
                self._bot.client.fetch_klines_cached(symbol, interval, limit=limit),
                timeout=8.0,
            )
        except DEFENSIVE_EXC as exc:
            LOG.debug(
                "market-state kline fetch failed | symbol=%s interval=%s error=%s",
                symbol,
                interval,
                exc,
            )
            return []
        if frame.is_empty() or "close" not in frame.columns:
            return []
        values = [self._safe_float(value) for value in frame["close"].to_list()]
        return [value for value in values if value > 0.0]

    async def _resolve_change_pct(
        self,
        symbol: str,
        interval: str,
        ticker_by_symbol: dict[str, dict[str, Any]],
    ) -> tuple[float, str, list[float]]:
        cached = self._cached_kline_change_pct(symbol, interval)
        if cached is not None:
            return cached * 100.0, "kline_cache", []
        limit_by_interval = {"15m": 220, "1h": 220, "4h": 220}
        closes = await self._fetch_close_values(
            symbol,
            interval,
            limit=limit_by_interval.get(interval, 220),
        )
        if len(closes) >= 2:
            return (closes[-1] / closes[-2] - 1.0) * 100.0, "rest_kline", closes
        ticker_change = self._ticker_change_pct(ticker_by_symbol.get(symbol))
        scale = {"15m": 1.0 / 96.0, "1h": 1.0 / 24.0, "4h": 1.0 / 6.0}.get(interval, 1.0)
        return ticker_change * scale, "ticker_24h_proxy", []

    @staticmethod
    def _ema(values: list[float], span: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (float(span) + 1.0)
        ema = values[0]
        for value in values[1:]:
            ema = value * alpha + ema * (1.0 - alpha)
        return ema

    async def _timeframe_line(
        self,
        symbol: str,
        interval: str,
        ticker_by_symbol: dict[str, dict[str, Any]],
    ) -> str:
        threshold = {"15m": 0.12, "1h": 0.35, "4h": 0.90}.get(interval, 0.35)
        change_pct, source, closes = await self._resolve_change_pct(
            symbol,
            interval,
            ticker_by_symbol,
        )
        trend, impulse = self._trend_bias_ru(change_pct, strong_threshold=threshold)
        trend_strength = "тренд выражен" if abs(change_pct) >= threshold else "тренд слабый"
        parts = [f"{interval}: {trend}", impulse, trend_strength]
        if interval == "15m":
            if not closes:
                closes = await self._fetch_close_values(symbol, interval, limit=220)
            if len(closes) >= 50:
                ema_span = min(200, len(closes))
                ema_value = self._ema(closes[-ema_span:], ema_span)
                price_state = "ниже EMA200" if closes[-1] < ema_value else "выше EMA200"
                parts.append(price_state)
        if source == "ticker_24h_proxy":
            parts.append("источник 24h proxy")
        return "; ".join(parts)

    @staticmethod
    def _returns(values: list[float]) -> list[float]:
        returns: list[float] = []
        for left, right in itertools.pairwise(values):
            if left > 0.0 and right > 0.0:
                returns.append((right / left) - 1.0)
        return returns

    @staticmethod
    def _correlation(left: list[float], right: list[float]) -> float | None:
        size = min(len(left), len(right))
        if size < 8:
            return None
        x = left[-size:]
        y = right[-size:]
        mean_x = sum(x) / size
        mean_y = sum(y) / size
        cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=False))
        var_x = sum((a - mean_x) ** 2 for a in x)
        var_y = sum((b - mean_y) ** 2 for b in y)
        if var_x <= 0.0 or var_y <= 0.0:
            return None
        value = cov / math.sqrt(var_x * var_y)
        return max(-1.0, min(1.0, value))

    @staticmethod
    def _corr_strength(value: float) -> str:
        abs_value = abs(value)
        if abs_value >= 0.75:
            return "high"
        if abs_value >= 0.40:
            return "weak"
        return "flat"

    async def _build_correlation_line(
        self,
        ticker_by_symbol: dict[str, dict[str, Any]],
        liquid_rows: list[dict[str, Any]],
    ) -> tuple[str, str]:
        btc_closes = await self._fetch_close_values("BTCUSDT", "1h", limit=190)
        btc_returns = self._returns(btc_closes)
        pairs: list[tuple[str, float]] = []
        narrative: list[str] = []

        async def add_symbol(label: str, symbol: str) -> None:
            if symbol not in ticker_by_symbol:
                return
            closes = await self._fetch_close_values(symbol, "1h", limit=190)
            corr = self._correlation(btc_returns, self._returns(closes))
            if corr is not None:
                pairs.append((label, corr))

        await add_symbol("ETH", "ETHUSDT")
        await add_symbol("PAXG", "PAXGUSDT")
        await add_symbol("XAU", "XAUUSDT")
        await add_symbol("XAG", "XAGUSDT")

        stable_bases = {"USDC", "BUSD", "FDUSD", "TUSD", "USDE", "DAI", "USDP", "PYUSD"}
        alt_symbols = [
            self._ticker_symbol(row)
            for row in liquid_rows
            if self._ticker_symbol(row) not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
            and self._ticker_symbol(row).removesuffix("USDT") not in stable_bases
        ][:5]
        alt_returns_grid: list[list[float]] = []
        for symbol in alt_symbols:
            closes = await self._fetch_close_values(symbol, "1h", limit=190)
            returns = self._returns(closes)
            if len(returns) >= 8:
                alt_returns_grid.append(returns)
        if alt_returns_grid:
            size = min(len(row) for row in alt_returns_grid)
            basket = [
                sum(row[-size + idx] for row in alt_returns_grid) / len(alt_returns_grid)
                for idx in range(size)
            ]
            corr = self._correlation(btc_returns, basket)
            if corr is not None:
                pairs.insert(1, ("ALTS", corr))

        if not pairs:
            btc_change = self._ticker_change_pct(ticker_by_symbol.get("BTCUSDT"))
            eth_change = self._ticker_change_pct(ticker_by_symbol.get("ETHUSDT"))
            proxy = 0.35 if btc_change * eth_change >= 0.0 else -0.35
            pairs.append(("ETH", proxy))
            narrative.append("corr: используется 24h co-direction proxy до прогрева 1h klines")

        rendered = [f"{label} {corr:+.2f} {self._corr_strength(corr)}" for label, corr in pairs]
        for label, corr in pairs:
            if label == "ETH":
                if corr >= 0.65:
                    narrative.append("ETH движется синхронно \u0441 BTC")
                elif corr <= -0.35:
                    narrative.append("ETH расходится \u0441 BTC")
            if label == "ALTS":
                if abs(corr) <= 0.25:
                    narrative.append("альт-бета распалась")
                elif corr >= 0.60:
                    narrative.append("альты синхронны \u0441 BTC")
            if label in {"PAXG", "XAU", "XAG"} and corr >= 0.40:
                narrative.append("металлы движутся вместе \u0441 BTC")
        if not narrative:
            narrative.append("корреляции умеренные; рынок без одного доминирующего драйвера")
        return "corr 1h/7d proxy: " + " | ".join(rendered), " | ".join(dict.fromkeys(narrative))

    @classmethod
    def _format_leaders(cls, rows: list[dict[str, Any]], *, reverse: bool) -> str:
        ranked = sorted(rows, key=cls._ticker_change_pct, reverse=reverse)[:5]
        if not ranked:
            return "нет liquid futures после фильтра объема"
        return ", ".join(
            f"{cls._ticker_symbol(row)} {cls._fmt_signed_pct_value(cls._ticker_change_pct(row))}"
            for row in ranked
        )

    async def _build_market_state_text(
        self,
        *,
        regime: MarketRegimeResult,
        macro_risk_mode: str,
        ticker_rows: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        ticker_by_symbol = {self._ticker_symbol(row): row for row in ticker_rows}
        liquid_rows = self._liquid_ticker_rows(ticker_rows)
        positive_count = sum(1 for row in liquid_rows if self._ticker_change_pct(row) > 0.0)
        liquid_count = len(liquid_rows)
        breadth_share = positive_count / liquid_count if liquid_count else 0.0
        btc_24h_pct = self._ticker_change_pct(ticker_by_symbol.get("BTCUSDT"))
        eth_24h_pct = self._ticker_change_pct(ticker_by_symbol.get("ETHUSDT"))
        sol_24h_pct = self._ticker_change_pct(ticker_by_symbol.get("SOLUSDT"))
        risk_label = self._risk_label(macro_risk_mode or regime.risk_on_off)
        fear_value, fear_label = self._fear_greed_proxy(
            breadth_share=breadth_share,
            btc_24h_pct=btc_24h_pct,
            regime=regime,
            funding_sentiment=regime.funding_sentiment,
        )
        if risk_label == "risk-off":
            practical = (
                "рынок больше поддерживает short или осторожный режим; long фильтровать строже"
            )
        elif risk_label == "risk-on":
            practical = (
                "рынок поддерживает continuation/breakout long; short требует сильного свипа"
            )
        else:
            practical = (
                "рынок смешанный; приоритет сетапам \u0441 подтвержденной ликвидностью и объемом"
            )

        total_quote_volume = sum(self._ticker_quote_volume(row) for row in liquid_rows) or 1.0
        stable_bases = {"USDC", "BUSD", "FDUSD", "TUSD", "USDE", "DAI", "USDP", "PYUSD"}
        btc_volume_share = (
            self._ticker_quote_volume(ticker_by_symbol.get("BTCUSDT")) / total_quote_volume * 100.0
        )
        eth_volume_share = (
            self._ticker_quote_volume(ticker_by_symbol.get("ETHUSDT")) / total_quote_volume * 100.0
        )
        sol_volume_share = (
            self._ticker_quote_volume(ticker_by_symbol.get("SOLUSDT")) / total_quote_volume * 100.0
        )
        stable_volume_share = (
            sum(
                self._ticker_quote_volume(row)
                for row in liquid_rows
                if self._ticker_symbol(row).removesuffix("USDT") in stable_bases
            )
            / total_quote_volume
            * 100.0
        )
        alt_volume_share = max(
            0.0,
            100.0 - btc_volume_share - eth_volume_share - sol_volume_share - stable_volume_share,
        )

        timeframe_results = await asyncio.gather(
            self._timeframe_line("BTCUSDT", "4h", ticker_by_symbol),
            self._timeframe_line("BTCUSDT", "1h", ticker_by_symbol),
            self._timeframe_line("BTCUSDT", "15m", ticker_by_symbol),
            return_exceptions=True,
        )
        timeframe_labels = ("4h", "1h", "15m")
        timeframe_lines: list[str] = []
        for label, result in zip(timeframe_labels, timeframe_results, strict=True):
            if isinstance(result, Exception):
                LOG.warning(
                    "market context timeframe line failed | symbol=BTCUSDT timeframe=%s error=%s",
                    label,
                    result,
                )
                timeframe_lines.append(f"BTCUSDT {label}: n/a")
            else:
                timeframe_lines.append(result)
        tf_4h, tf_1h, tf_15m = timeframe_lines
        intraday_note = self._intraday_vs_24h_note(
            btc_24h_pct=btc_24h_pct,
            tf_1h=tf_1h,
            tf_15m=tf_15m,
        )
        corr_line, corr_narrative = await self._build_correlation_line(
            ticker_by_symbol,
            liquid_rows,
        )
        paxg_change = self._ticker_change_pct(ticker_by_symbol.get("PAXGUSDT"))
        if "PAXGUSDT" in ticker_by_symbol:
            macro_line = f"PAXG {self._fmt_signed_pct_value(paxg_change)} | mode {risk_label}"
        else:
            macro_line = f"mode {risk_label} по Binance breadth/BTC/funding proxy"

        tracked_line = (
            f"Сопровождение: active {int(stats.get('active', 0) or 0)} | "
            f"pending {int(stats.get('pending', 0) or 0)}"
        )
        lines = [
            "🧭 <b>Контекст рынка</b>",
            (
                f"Итог: <code>{html.escape(risk_label)}</code>; "
                f"fear/greed proxy <code>{fear_value} ({html.escape(fear_label)})</code>"
            ),
            f"Практически: {html.escape(practical)}.",
            (
                "Ширина рынка: "
                f"<code>{positive_count}</code> из <code>{liquid_count}</code> "
                f"ликвидных в плюсе "
                f"(<code>{self._fmt_unsigned_pct_value(breadth_share * 100.0, digits=0)}</code>)"
            ),
            html.escape(tf_4h),
            html.escape(tf_1h),
            html.escape(tf_15m),
            *([html.escape(intraday_note)] if intraday_note else []),
            (
                "Крипто-драйверы: "
                f"BTC <code>{self._fmt_signed_pct_value(btc_24h_pct)}</code> | "
                f"ETH <code>{self._fmt_signed_pct_value(eth_24h_pct)}</code> | "
                f"SOL <code>{self._fmt_signed_pct_value(sol_24h_pct)}</code>"
            ),
            (
                "Доминация фьючерсного объема: "
                f"BTC <code>{self._fmt_unsigned_pct_value(btc_volume_share)}</code> | "
                f"ETH <code>{self._fmt_unsigned_pct_value(eth_volume_share)}</code> | "
                f"SOL <code>{self._fmt_unsigned_pct_value(sol_volume_share)}</code> | "
                f"альты <code>{self._fmt_unsigned_pct_value(alt_volume_share)}</code> | "
                f"стейбл-базы <code>{self._fmt_unsigned_pct_value(stable_volume_share)}</code>"
            ),
            f"Макро-прокси: <code>{html.escape(macro_line)}</code>",
            html.escape(corr_line),
            html.escape(corr_narrative),
            (
                "Лидеры 24ч (liquid futures): "
                f"<code>{html.escape(self._format_leaders(liquid_rows, reverse=True))}</code>"
            ),
            (
                "Аутсайдеры 24ч (liquid futures): "
                f"<code>{html.escape(self._format_leaders(liquid_rows, reverse=False))}</code>"
            ),
            html.escape(tracked_line),
        ]
        display_snapshot = {
            "risk_label": risk_label,
            "fear_greed_value": fear_value,
            "fear_greed_label": fear_label,
            "practical": practical,
            "breadth_positive": positive_count,
            "breadth_total": liquid_count,
            "breadth_pct": round(breadth_share * 100.0, 1),
            "regime": regime.regime,
            "tf_4h": tf_4h,
            "tf_1h": tf_1h,
            "tf_15m": tf_15m,
            "intraday_note": intraday_note,
            "btc_24h_pct": btc_24h_pct,
            "eth_24h_pct": eth_24h_pct,
            "sol_24h_pct": sol_24h_pct,
            "volume_btc_pct": btc_volume_share,
            "volume_eth_pct": eth_volume_share,
            "volume_sol_pct": sol_volume_share,
            "volume_alts_pct": alt_volume_share,
            "volume_stables_pct": stable_volume_share,
            "macro_line": macro_line,
            "corr_line": corr_line,
            "corr_narrative": corr_narrative,
            "leaders": self._format_leaders(liquid_rows, reverse=True),
            "laggards": self._format_leaders(liquid_rows, reverse=False),
            "tracking_active": int(stats.get("active", 0) or 0),
            "tracking_pending": int(stats.get("pending", 0) or 0),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        return "\n".join(lines), display_snapshot

    async def build_market_state_html(self, *, force: bool = False) -> str:
        """Rebuild rich market HTML for operator console (uses live tickers when possible)."""
        if not force and self._last_market_state_html:
            return self._last_market_state_html
        async with self._bot._shortlist_lock:
            shortlist = list(self._bot._shortlist)
        if not shortlist or not isinstance(self._bot.client, BinanceFuturesMarketData):
            return self._last_market_state_html or ""
        try:
            all_tickers = await self._bot.client.fetch_ticker_24h()
            regime = self._bot.market_regime._last_result
            if regime is None:
                return self._last_market_state_html or ""
            macro_risk_mode = self._macro_proxy_mode(regime)
            intelligence_snapshot = (
                self._bot.intelligence.latest_snapshot
                if self._bot.intelligence is not None
                else None
            )
            if intelligence_snapshot:
                macro_snapshot = cast("dict[str, Any]", intelligence_snapshot.get("macro") or {})
                snapshot_mode = str(macro_snapshot.get("risk_mode") or "").strip()
                if snapshot_mode and not snapshot_mode.startswith("disabled_"):
                    macro_risk_mode = snapshot_mode
            stats: dict[str, Any] = {}
            with contextlib.suppress(DEFENSIVE_EXC):
                stats = await asyncio.wait_for(
                    self._bot._modern_repo.get_tracking_stats(),
                    timeout=1.0,
                )
            html_text, display_snapshot = await self._build_market_state_text(
                regime=regime,
                macro_risk_mode=macro_risk_mode,
                ticker_rows=all_tickers,
                stats=stats,
            )
            self._last_market_state_html = html_text
            self._last_display_snapshot = display_snapshot
        except DEFENSIVE_EXC:
            LOG.debug("operator market html rebuild failed", exc_info=True)
            return self._last_market_state_html or ""
        else:
            return html_text

    async def _maybe_send_market_state_update(
        self,
        regime: MarketRegimeResult,
        *,
        benchmark_context: dict[str, dict[str, Any]],
        macro_risk_mode: str,
        previous_regime: str | None,
        ticker_rows: list[dict[str, Any]],
        shortlist: list[UniverseSymbol],
    ) -> None:
        from bot.delivery.telegram_routing import operator_dm_enabled, send_operator_html

        if not operator_dm_enabled(self._bot, "send_market_context"):
            return

        btc = benchmark_context.get("BTCUSDT", {})
        btc_1h = btc.get("pct_1h")
        btc_4h = btc.get("pct_4h")
        btc_24h = btc.get("pct_24h")
        try:
            btc_1h_abs = abs(float(btc_1h))
        except (TypeError, ValueError):
            btc_1h_abs = 0.0
        try:
            btc_4h_abs = abs(float(btc_4h))
        except (TypeError, ValueError):
            btc_4h_abs = 0.0
        try:
            btc_24h_abs = abs(float(btc_24h))
        except (TypeError, ValueError):
            btc_24h_abs = 0.0

        regime_changed = previous_regime != regime.regime
        first_state = previous_regime is None
        btc_move_watch = btc_1h_abs >= 0.006 or btc_4h_abs >= 0.015 or btc_24h_abs >= 0.03
        if not (first_state or regime_changed or btc_move_watch):
            return

        reason = "startup" if first_state else ("regime_change" if regime_changed else "btc_watch")
        key = ":".join(
            [
                reason,
                regime.regime,
                regime.btc_bias,
                regime.eth_bias,
                str(round(btc_1h_abs * 100, 1)),
                str(round(btc_4h_abs * 100, 1)),
                str(round(btc_24h_abs * 100, 1)),
                str(len(shortlist)),
            ]
        )
        now = time.monotonic()
        if (
            key == self._last_market_state_alert_key
            and (now - self._last_market_state_alert_at) < 900.0
        ):
            return

        stats: dict[str, Any] = {}
        try:
            stats = await asyncio.wait_for(self._bot._modern_repo.get_tracking_stats(), timeout=1.0)
        except DEFENSIVE_EXC as exc:
            LOG.debug("market-state tracking stats unavailable: %s", exc)

        text, _display = await self._build_market_state_text(
            regime=regime,
            macro_risk_mode=macro_risk_mode,
            ticker_rows=ticker_rows,
            stats=stats,
        )
        try:
            sent = await send_operator_html(self._bot, text)
            if sent:
                self._last_market_state_alert_key = key
                self._last_market_state_alert_at = now
        except DEFENSIVE_EXC as exc:
            LOG.debug("market-state operator DM failed: %s", exc)

    def compute_price_bias(self, symbol: str) -> str:
        if self._bot._ws_manager is None:
            return "neutral"

        def bias_from_change(pct: float, threshold: float) -> str:
            if pct > threshold:
                return "uptrend"
            if pct < -threshold:
                return "downtrend"
            return "neutral"

        ticker = self._bot._ws_manager.get_ticker_snapshot(symbol)
        if ticker:
            try:
                pct_24h = float(ticker.get("price_change_percent") or 0.0) / 100.0
                bias_24h = bias_from_change(pct_24h, 0.015)
                if bias_24h != "neutral":
                    return bias_24h
            except (TypeError, ValueError) as exc:
                LOG.debug("ticker price_change_percent invalid for %s: %s", symbol, exc)

        for interval, lookback, threshold in (
            ("4h", 6, 0.012),
            ("1h", 12, 0.008),
            ("15m", 16, 0.006),
        ):
            klines = self._bot._ws_manager.get_kline_cache(symbol, interval)
            if not klines or len(klines) < lookback + 1:
                continue
            try:
                c_old = float(klines[-(lookback + 1)]["close"])
                c_new = float(klines[-1]["close"])
                if c_old > 0 and c_new > 0:
                    bias = bias_from_change((c_new - c_old) / c_old, threshold)
                    if bias != "neutral":
                        return bias
            except (KeyError, TypeError, ValueError):
                continue

        for interval, threshold in (
            ("4h", 0.008),
            ("1h", 0.0035),
            ("15m", 0.0018),
            ("5m", 0.0012),
        ):
            klines = self._bot._ws_manager.get_kline_cache(symbol, interval)
            if not klines or len(klines) < 2:
                continue
            try:
                c1 = float(klines[-2]["close"])
                c2 = float(klines[-1]["close"])
                if c1 > 0 and c2 > 0:
                    bias = bias_from_change((c2 - c1) / c1, threshold)
                    if bias != "neutral":
                        return bias
            except (KeyError, TypeError, ValueError):
                continue
        return "neutral"


async def run_market_regime_loop(updater: MarketContextUpdater) -> None:
    """Background market-regime refresh loop (started from SignalBot.run_forever)."""
    await updater.market_regime_periodic()


async def run_public_intelligence_loop(updater: MarketContextUpdater) -> None:
    """Background public-intelligence loop (started from SignalBot.run_forever)."""
    await updater.public_intelligence_periodic()
