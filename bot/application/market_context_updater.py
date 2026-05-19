from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from bot.market_data import BinanceFuturesMarketData
from bot.market_regime import MarketRegimeResult
from bot.domain.schemas import UniverseSymbol

if TYPE_CHECKING:
    from bot.application.bot import SignalBot


LOG = logging.getLogger("bot.application.bot")


class MarketContextUpdater:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot
        self._last_regime: str | None = None
        self._last_market_state_alert_key: str | None = None
        self._last_market_state_alert_at: float = 0.0

    async def market_regime_periodic(self) -> None:
        await asyncio.sleep(10)
        while not self._bot._shutdown.is_set():
            try:
                async with self._bot._shortlist_lock:
                    shortlist = list(self._bot._shortlist)
                if shortlist:
                    await self.update_memory_market_context(shortlist)
                    LOG.debug("market regime periodic update completed")
            except Exception as exc:
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
                        cast(dict[str, Any], snapshot.get("barrier") or {}).get(
                            "long_barrier_triggered"
                        ),
                        cast(dict[str, Any], snapshot.get("barrier") or {}).get(
                            "short_barrier_triggered"
                        ),
                        cast(dict[str, Any], snapshot.get("macro") or {}).get("risk_mode"),
                    )
            except Exception:
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

        barrier = cast(dict[str, Any], snapshot.get("barrier") or {})
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
                        note=";".join(cast(list[str], smart_exit.get("reasons") or [])[:6]),
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
                cached = self._bot.client._funding_rate_cache.get(item.symbol)
                if cached is None:
                    continue
                _, fr = cached
                funding_rates[item.symbol] = fr
                if fr >= extreme_threshold:
                    high_funding.append(item.symbol)
                elif fr <= -extreme_threshold:
                    low_funding.append(item.symbol)

            btc_bias = "neutral"
            eth_bias = "neutral"
            if self._bot._ws_manager is not None:
                for sym, bias_attr in [
                    ("BTCUSDT", "btc_bias"),
                    ("ETHUSDT", "eth_bias"),
                ]:
                    bias = self.compute_price_bias(sym)
                    if bias_attr == "btc_bias":
                        btc_bias = bias
                    else:
                        eth_bias = bias

            benchmark_context: dict[str, dict[str, Any]] = {}
            for sym, bias in [("BTCUSDT", btc_bias), ("ETHUSDT", eth_bias)]:
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

            ticker_data: list[dict[str, Any]] = []
            all_tickers = await self._bot.client.fetch_ticker_24h()
            ticker_dict = {t.get("symbol"): t for t in all_tickers if isinstance(t, dict)}
            for sym in ("BTCUSDT", "ETHUSDT"):
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
            macro_risk_mode = (
                "disabled_binance_only"
                if self._bot.settings.intelligence.source_policy == "binance_only"
                else "unknown"
            )
            if intelligence_snapshot:
                macro_snapshot = cast(dict[str, Any], intelligence_snapshot.get("macro") or {})
                macro_risk_mode = str(macro_snapshot.get("risk_mode") or macro_risk_mode)
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
                intelligence_snapshot=intelligence_snapshot,
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
            )

        except Exception:
            LOG.exception("memory market context update failed")

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
            return "n/a"
        return f"{numeric * 100:+.2f}%"

    async def _maybe_send_market_state_update(
        self,
        regime: MarketRegimeResult,
        *,
        benchmark_context: dict[str, dict[str, Any]],
        macro_risk_mode: str,
        previous_regime: str | None,
    ) -> None:
        notifiers = getattr(self._bot.settings, "notifiers", None)
        if str(getattr(notifiers, "provider", "telegram")) != "telegram":
            return
        sender = getattr(self._bot.telegram, "send_html", None)
        if not callable(sender):
            return

        btc = benchmark_context.get("BTCUSDT", {})
        eth = benchmark_context.get("ETHUSDT", {})
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
            ]
        )
        now = time.monotonic()
        if key == self._last_market_state_alert_key and (
            now - self._last_market_state_alert_at
        ) < 900.0:
            return

        stats: dict[str, Any] = {}
        try:
            stats = await asyncio.wait_for(self._bot._modern_repo.get_tracking_stats(), timeout=1.0)
        except Exception as exc:
            LOG.debug("market-state tracking stats unavailable: %s", exc)

        btc_note = "neutral"
        if regime.btc_bias in {"downtrend", "bear"} or (
            isinstance(btc_1h, (int, float)) and btc_1h <= -0.006
        ):
            btc_note = "watch downside pressure"
        elif regime.btc_bias in {"uptrend", "bull"} or (
            isinstance(btc_1h, (int, float)) and btc_1h >= 0.006
        ):
            btc_note = "watch upside pressure"

        text = "\n".join(
            [
                f"<b>Market State</b> <code>{html.escape(reason)}</code>",
                (
                    f"Regime: <code>{html.escape(regime.regime)}</code> | "
                    f"strength <code>{float(regime.strength):.2f}</code> | "
                    f"confidence <code>{float(regime.confidence):.2f}</code>"
                ),
                (
                    f"BTC: <code>{html.escape(regime.btc_bias)}</code> | "
                    f"1h <code>{self._fmt_pct(btc_1h)}</code> | "
                    f"4h <code>{self._fmt_pct(btc_4h)}</code> | "
                    f"24h <code>{self._fmt_pct(btc_24h)}</code>"
                ),
                (
                    f"ETH: <code>{html.escape(regime.eth_bias)}</code> | "
                    f"1h <code>{self._fmt_pct(eth.get('pct_1h'))}</code> | "
                    f"24h <code>{self._fmt_pct(eth.get('pct_24h'))}</code>"
                ),
                (
                    f"BTC note: <code>{html.escape(btc_note)}</code> | "
                    f"phase <code>{html.escape(str(regime.btc_phase))}</code>"
                ),
                (
                    f"Alt index: <code>{float(regime.altcoin_season_index):.0f}/100</code> | "
                    f"risk <code>{html.escape(str(regime.risk_on_off))}</code> | "
                    f"macro <code>{html.escape(str(macro_risk_mode))}</code>"
                ),
                (
                    f"Tracked: active <code>{int(stats.get('active', 0) or 0)}</code> | "
                    f"pending <code>{int(stats.get('pending', 0) or 0)}</code>"
                ),
            ]
        )
        try:
            await sender(text)
            self._last_market_state_alert_key = key
            self._last_market_state_alert_at = now
        except Exception as exc:
            LOG.debug("market-state telegram update failed: %s", exc)

    def compute_price_bias(self, symbol: str) -> str:
        if self._bot._ws_manager is None:
            return "neutral"

        def bias_from_change(pct: float, threshold: float) -> str:
            if pct > threshold:
                return "uptrend"
            if pct < -threshold:
                return "downtrend"
            return "neutral"

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
        ticker = self._bot._ws_manager.get_ticker_snapshot(symbol)
        if ticker:
            try:
                pct_24h = float(ticker.get("price_change_percent") or 0.0) / 100.0
                return bias_from_change(pct_24h, 0.02)
            except (TypeError, ValueError) as exc:
                LOG.debug("ticker price_change_percent invalid for %s: %s", symbol, exc)
        return "neutral"
