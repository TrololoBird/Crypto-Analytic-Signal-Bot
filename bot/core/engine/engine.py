"""Signal engine for orchestrating strategy calculations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections.abc import Callable
from typing import Any, cast

from .registry import StrategyRegistry
from .base import SignalResult
from ...domain.strategies import StrategyDecision
from ...domain.schemas import PreparedSymbol, Signal
from ...domain.config import BotSettings
from ...strategy_asset_fit import asset_fit_reject_reason, market_context_from_prepared
from ..runtime_errors import classify_runtime_error

LOG = logging.getLogger("bot.core.engine.engine")
_STRATEGY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(8, min(32, (os.cpu_count() or 4) * 4)),
    thread_name_prefix="signal-strategy",
)
_WARMED_EXECUTOR_WORKERS = 0


def _executor_noop() -> None:
    return None


class SignalEngine:
    """Engine for calculating signals from multiple strategies.

    Features:
    - Parallel strategy execution with asyncio
    - Performance tracking
    - Error isolation (one strategy failure doesn't break others)
    - Configurable timeout per strategy
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        settings: BotSettings,
        timeout_seconds: float | None = None,
        strategy_concurrency: int | None = None,
    ):
        self._registry = registry
        self._settings = settings
        runtime = getattr(self._settings, "runtime", None)
        configured_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(getattr(runtime, "strategy_timeout_seconds", 5.0))
        )
        self._timeout = max(0.5, configured_timeout)
        configured_concurrency = strategy_concurrency
        if configured_concurrency is None:
            configured_concurrency = getattr(runtime, "strategy_concurrency", None)
        if configured_concurrency is None:
            configured_concurrency = getattr(runtime, "analysis_concurrency", 10)
        self._strategy_concurrency = max(1, int(configured_concurrency))
        self._semaphore = asyncio.Semaphore(self._strategy_concurrency)
        self._executor_warmed = False
        self._executor_warm_lock = asyncio.Lock()

    async def calculate_all(self, prepared: PreparedSymbol) -> list[SignalResult]:
        """Calculate signals from all enabled strategies.

        Args:
            prepared: Prepared symbol data

        Returns:
            List of SignalResult from all strategies
        """
        symbol = prepared.symbol if prepared else "unknown"
        strategies = self._registry.get_enabled()
        routing_skips: list[SignalResult] = []
        universe = getattr(prepared, "universe", None)
        strategy_fits = set(getattr(universe, "strategy_fits", ()) or ())
        shortlist_score = getattr(universe, "shortlist_score", None)
        is_shortlist_asset = shortlist_score is not None
        if is_shortlist_asset:
            LOG.debug(
                "%s: shortlist routing expanded to all enabled strategies | strategy_fits=%d",
                symbol,
                len(strategy_fits),
            )
        elif strategy_fits:
            routed: list[Any] = []
            emit_routing_skips = bool(
                getattr(
                    getattr(self._settings, "runtime", None),
                    "emit_strategy_routing_skips",
                    True,
                )
            )
            for strategy in strategies:
                if strategy.strategy_id in strategy_fits:
                    routed.append(strategy)
                elif emit_routing_skips:
                    decision = self._build_routing_skip_decision(strategy, prepared, strategy_fits)
                    routing_skips.append(
                        SignalResult(
                            setup_id=strategy.strategy_id,
                            signal=None,
                            decision=decision,
                            metadata={
                                "setup_id": strategy.strategy_id,
                                "reason": decision.reason_code,
                                "routed_strategy_count": len(strategy_fits),
                            },
                            calculation_time_ms=0.0,
                        )
                    )
            strategies = routed
        LOG.info("%s: calculate_all called | strategies=%d", symbol, len(strategies))

        if not strategies:
            LOG.warning("%s: No enabled strategies to calculate", symbol)
            return routing_skips

        # Check which strategies can calculate
        can_calculate_count = 0
        for s in strategies:
            if s.can_calculate(prepared):
                can_calculate_count += 1

        LOG.debug(
            "%s: strategies can_calculate=%d/%d",
            symbol,
            can_calculate_count,
            len(strategies),
        )
        await self._ensure_executor_warmed(min(len(strategies), self._strategy_concurrency))

        pending = [
            asyncio.create_task(
                self._calculate_one(strategy, prepared),
                name=f"engine:{symbol}:{strategy.strategy_id}",
            )
            for strategy in strategies
        ]
        results = await asyncio.gather(*pending, return_exceptions=True)

        # Process results and log errors
        signal_results: list[SignalResult] = list(routing_skips)
        signals_found = 0
        errors = 0

        for strategy, result in zip(strategies, results, strict=True):
            if isinstance(result, BaseException):
                error_class = classify_runtime_error(result)
                LOG.error(
                    "%s: Strategy %s failed: %s | error_class=%s",
                    symbol,
                    strategy.strategy_id,
                    result,
                    error_class,
                )
                decision = StrategyDecision.error_result(
                    setup_id=strategy.strategy_id,
                    reason_code=f"{error_class}.error",
                    error=str(result),
                    stage="engine",
                    details={"symbol": symbol, "error_class": error_class},
                )
                signal_results.append(
                    SignalResult(
                        setup_id=strategy.strategy_id,
                        signal=None,
                        decision=decision,
                        error=decision.error,
                        calculation_time_ms=0.0,
                        metadata={"setup_id": strategy.strategy_id},
                    )
                )
                errors += 1
            else:
                signal_results.append(result)
                if result.signal is not None:
                    signals_found += 1

        LOG.info(
            "%s: calculate_all complete | results=%d signals=%d errors=%d",
            symbol,
            len(signal_results),
            signals_found,
            errors,
        )

        return signal_results

    async def calculate_one(
        self, strategy_id: str, prepared: PreparedSymbol
    ) -> SignalResult | None:
        """Calculate signal from specific strategy.

        Args:
            strategy_id: Strategy ID to calculate
            prepared: Prepared symbol data

        Returns:
            SignalResult or None if strategy not found/disabled
        """
        strategy = self._registry.get(strategy_id)
        if strategy is None:
            LOG.warning("Strategy %s not found", strategy_id)
            return None

        if not self._registry.is_enabled(strategy_id):
            LOG.debug("Strategy %s is disabled", strategy_id)
            return None

        return await self._calculate_one(strategy, prepared)

    async def _calculate_one(
        self,
        strategy: Any,  # AbstractStrategy
        prepared: PreparedSymbol,
    ) -> SignalResult:
        """Calculate signal from single strategy with timeout and error handling."""
        strategy_id = strategy.strategy_id
        symbol = prepared.symbol if prepared else "unknown"
        queued_at = time.perf_counter()

        async with self._semaphore:
            start_time = time.perf_counter()
            queue_wait_ms = (start_time - queued_at) * 1000.0

            try:
                # Check if strategy can calculate
                if not strategy.can_calculate(prepared):
                    LOG.debug("%s: %s skipped - insufficient data", symbol, strategy_id)
                    decision = self._build_skip_decision(strategy, prepared)
                    return SignalResult(
                        setup_id=strategy_id,
                        signal=None,
                        decision=decision,
                        metadata={
                            "setup_id": strategy_id,
                            "reason": decision.reason_code,
                            "queue_wait_ms": queue_wait_ms,
                            "compute_ms": 0.0,
                        },
                        calculation_time_ms=0.0,
                    )

                # Run calculation with timeout
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(_STRATEGY_EXECUTOR, strategy.calculate, prepared),
                    timeout=self._timeout,
                )

                elapsed_ms = (time.perf_counter() - start_time) * 1000

                # Record performance
                self._registry.record_performance(
                    strategy_id,
                    elapsed_ms,
                    error=bool(result.decision and result.decision.is_error),
                )

                # Update result with accurate timing
                result.calculation_time_ms = elapsed_ms
                result.metadata.setdefault("queue_wait_ms", queue_wait_ms)
                result.metadata.setdefault("compute_ms", elapsed_ms)

                LOG.debug(
                    "Strategy %s calculated in %.2fms (queue_wait=%.2fms signal=%s)",
                    strategy_id,
                    elapsed_ms,
                    queue_wait_ms,
                    result.signal is not None,
                )

                return cast(SignalResult, result)

            except asyncio.TimeoutError:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                LOG.error("Strategy %s timed out after %.2fs", strategy_id, self._timeout)
                self._registry.record_performance(strategy_id, elapsed_ms, error=True)
                decision = StrategyDecision.error_result(
                    setup_id=strategy_id,
                    reason_code="engine.timeout",
                    error=f"timeout after {self._timeout}s",
                    stage="engine",
                    details={
                        "timeout_seconds": self._timeout,
                        "symbol": symbol,
                        "error_class": "engine",
                        "queue_wait_ms": queue_wait_ms,
                    },
                )
                return SignalResult(
                    setup_id=strategy_id,
                    signal=None,
                    decision=decision,
                    error=decision.error,
                    calculation_time_ms=elapsed_ms,
                    metadata={
                        "setup_id": strategy_id,
                        "queue_wait_ms": queue_wait_ms,
                        "compute_ms": elapsed_ms,
                    },
                )

            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                error_class = classify_runtime_error(exc)
                LOG.exception(
                    "Strategy %s failed: %s | error_class=%s",
                    strategy_id,
                    exc,
                    error_class,
                )
                self._registry.record_performance(strategy_id, elapsed_ms, error=True)
                decision = StrategyDecision.error_result(
                    setup_id=strategy_id,
                    reason_code=f"{error_class}.error",
                    error=str(exc),
                    stage="engine",
                    details={
                        "symbol": symbol,
                        "exception_type": type(exc).__name__,
                        "error_class": error_class,
                    },
                )
                return SignalResult(
                    setup_id=strategy_id,
                    signal=None,
                    decision=decision,
                    error=decision.error,
                    calculation_time_ms=elapsed_ms,
                    metadata={
                        "setup_id": strategy_id,
                        "queue_wait_ms": queue_wait_ms,
                        "compute_ms": elapsed_ms,
                    },
                )

    def _build_skip_decision(self, strategy: Any, prepared: PreparedSymbol) -> StrategyDecision:
        strategy_id = strategy.strategy_id
        metadata = getattr(strategy, "metadata", None)
        min_history_bars = getattr(metadata, "min_history_bars", 0)
        required_context = list(getattr(metadata, "required_context", ()) or ())
        missing_fields: list[str] = []
        details: dict[str, Any] = {"required_context": required_context}
        reason_code = "data.insufficient_input"

        asset_fit_reason = asset_fit_reject_reason(
            strategy_id,
            prepared.symbol,
            market_context_from_prepared(prepared),
            settings=self._settings,
        )
        if asset_fit_reason is not None:
            reason_code = asset_fit_reason
            asset_fit = getattr(strategy, "asset_fit", None)
            to_dict = getattr(asset_fit, "to_dict", None)
            if callable(to_dict):
                details["asset_fit"] = cast(Callable[[], dict[str, Any]], to_dict)()
        elif prepared.work_1h is None or prepared.work_1h.is_empty():
            missing_fields.append("work_1h")
            reason_code = "data.work_1h_missing"
        elif int(prepared.work_1h.height) < int(min_history_bars):
            reason_code = "data.work_1h_insufficient_history"
            details["available_1h_bars"] = int(prepared.work_1h.height)
            details["required_1h_bars"] = int(min_history_bars)
        elif getattr(metadata, "requires_oi", False) and prepared.oi_current is None:
            missing_fields.append("oi_current")
            reason_code = "data.oi_current_missing"
        elif getattr(metadata, "requires_funding", False) and prepared.funding_rate is None:
            missing_fields.append("funding_rate")
            reason_code = "data.funding_rate_missing"

        return StrategyDecision.skip(
            setup_id=strategy_id,
            reason_code=reason_code,
            details=details,
            missing_fields=tuple(sorted(set(missing_fields))),
        )

    def _build_routing_skip_decision(
        self,
        strategy: Any,
        prepared: PreparedSymbol,
        strategy_fits: set[str],
    ) -> StrategyDecision:
        metadata = getattr(strategy, "metadata", None)
        return StrategyDecision.skip(
            setup_id=strategy.strategy_id,
            reason_code="asset_fit.shortlist_not_routed",
            details={
                "symbol": prepared.symbol,
                "routed_strategy_count": len(strategy_fits),
                "routed_strategies": sorted(strategy_fits),
                "status": getattr(metadata, "status", "unknown"),
                "risk_profile": getattr(metadata, "risk_profile", "unknown"),
            },
        )

    async def _ensure_executor_warmed(self, worker_count: int) -> None:
        global _WARMED_EXECUTOR_WORKERS
        if self._executor_warmed or worker_count <= 0:
            return
        async with self._executor_warm_lock:
            if self._executor_warmed:
                return
            if _WARMED_EXECUTOR_WORKERS >= worker_count:
                self._executor_warmed = True
                return
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *[
                    loop.run_in_executor(_STRATEGY_EXECUTOR, _executor_noop)
                    for _ in range(worker_count - _WARMED_EXECUTOR_WORKERS)
                ]
            )
            _WARMED_EXECUTOR_WORKERS = max(_WARMED_EXECUTOR_WORKERS, worker_count)
            self._executor_warmed = True

    def close(self) -> None:
        return None

    def get_best_signal(self, results: list[SignalResult]) -> Signal | None:
        """Select best signal from multiple results based on score.

        Args:
            results: List of SignalResult from strategies

        Returns:
            Best Signal or None if no valid signals
        """
        valid_signals = [r.signal for r in results if r.is_valid and r.signal is not None]

        if not valid_signals:
            return None

        # Sort by score descending
        valid_signals.sort(key=lambda s: s.score, reverse=True)

        # Return highest scored signal
        return valid_signals[0]

    def get_signals_above_threshold(
        self, results: list[SignalResult], min_score: float = 0.6
    ) -> list[Signal]:
        """Get all signals above score threshold.

        Args:
            results: List of SignalResult
            min_score: Minimum score to include

        Returns:
            List of Signals meeting threshold
        """
        signals = []
        for result in results:
            if result.is_valid and result.signal is not None:
                if result.signal.score >= min_score:
                    signals.append(result.signal)

        # Sort by score descending
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def get_engine_stats(self) -> dict[str, Any]:
        """Get engine statistics."""
        enabled_count = len(self._registry.get_enabled())
        total_count = len(self._registry)

        return {
            "enabled_strategies": enabled_count,
            "total_strategies": total_count,
            "timeout_seconds": self._timeout,
            "semaphore_limit": self._strategy_concurrency,
        }
