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
from ..domain.strategies import StrategyDecision
from ..domain.schemas import PreparedSymbol, Signal
from ..domain.config import BotSettings
from ..market.fit import asset_fit_reject_reason, market_context_from_prepared
from ..core.runtime_errors import classify_runtime_error

LOG = logging.getLogger("bot.engine.engine")
def _default_executor_workers() -> int:
    cpus = os.cpu_count() or 4
    return max(2, min(16, cpus * 2))


_STRATEGY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_default_executor_workers(),
    thread_name_prefix="signal-strategy",
)
_WARMED_EXECUTOR_WORKERS = 0
_executor_timeout_count = 0
_executor_timeout_by_strategy: dict[str, int] = {}


def _executor_noop() -> None:
    return None


def _has_oi_context(prepared: PreparedSymbol) -> bool:
    return prepared.oi_current is not None or prepared.oi_change_pct is not None


def _missing_required_features(prepared: PreparedSymbol, features: list[str]) -> tuple[str, ...]:
    if not features:
        return ()
    columns: set[str] = set()
    for frame_name in ("work_15m", "work_1h", "work_4h"):
        frame = getattr(prepared, frame_name, None)
        if frame is not None:
            columns.update(frame.columns)
    return tuple(feature for feature in features if feature not in columns)


def _missing_required_enrichment(prepared: PreparedSymbol, fields: list[str]) -> tuple[str, ...]:
    return tuple(field for field in fields if getattr(prepared, field, None) is None)


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
        feature_flags: Any | None = None,
    ):
        self._registry = registry
        self._settings = settings
        self._feature_flags = feature_flags
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
        self._max_queue_wait_seconds = max(
            0.0,
            float(getattr(runtime, "max_strategy_queue_wait_seconds", 45.0)),
        )
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
        pinned_symbols = {
            str(item).strip().upper()
            for item in getattr(getattr(self._settings, "universe", None), "pinned_symbols", ())
        }
        is_pinned_symbol = symbol.upper() in pinned_symbols
        route_all_on_shortlist = bool(
            getattr(getattr(self._settings, "runtime", None), "route_all_enabled_strategies", True)
        )
        if not strategy_fits and not is_pinned_symbol:
            LOG.warning(
                "%s: strategy_fits is EMPTY - routing coverage is degraded for this symbol. "
                "Check _strategy_fits_for_row() in universe.py",
                symbol,
            )
        emit_routing_skips = bool(
            getattr(
                getattr(self._settings, "runtime", None),
                "emit_strategy_routing_skips",
                True,
            )
        )
        if route_all_on_shortlist and (is_shortlist_asset or is_pinned_symbol):
            LOG.debug(
                "%s: shortlist routing expanded to all enabled strategies | strategy_fits=%d",
                symbol,
                len(strategy_fits),
            )
        elif strategy_fits and not route_all_on_shortlist:
            routed: list[Any] = []
            for strategy in strategies:
                if strategy.strategy_id in strategy_fits:
                    routed.append(strategy)
                elif emit_routing_skips:
                    decision = self._build_routing_skip_decision(strategy, prepared, strategy_fits)
                    if len(routing_skips) < 3:
                        LOG.info(
                            "%s: strategy not routed | setup=%s strategy_fits_count=%d",
                            symbol,
                            strategy.strategy_id,
                            len(strategy_fits),
                        )
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
        scheduled: list[Any] = []
        schedule_skip_results: list[SignalResult] = []
        for strategy in strategies:
            if self._strategy_is_active_for_symbol(strategy, prepared):
                scheduled.append(strategy)
            else:
                decision = StrategyDecision.skip(
                    setup_id=strategy.strategy_id,
                    reason_code="runtime.strategy_schedule_inactive",
                    details={
                        "symbol": symbol,
                        "strategy_id": strategy.strategy_id,
                    },
                )
                schedule_skip_results.append(
                    SignalResult(
                        setup_id=strategy.strategy_id,
                        signal=None,
                        decision=decision,
                        metadata={
                            "setup_id": strategy.strategy_id,
                            "reason": decision.reason_code,
                            "queue_wait_ms": 0.0,
                            "compute_ms": 0.0,
                        },
                        calculation_time_ms=0.0,
                    )
                )
        if schedule_skip_results:
            LOG.debug(
                "%s: strategy schedule skipped without detector telemetry | skipped=%d",
                symbol,
                len(schedule_skip_results),
            )
            routing_skips.extend(schedule_skip_results)
        strategies = scheduled
        LOG.info("%s: calculate_all called | strategies=%d", symbol, len(strategies))

        if not strategies:
            LOG.debug("%s: No enabled strategies to calculate after routing/schedule", symbol)
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

        per_symbol_semaphore = asyncio.Semaphore(self._strategy_concurrency)
        pending = [
            asyncio.create_task(
                self._calculate_one(strategy, prepared, semaphore=per_symbol_semaphore),
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
            LOG.error("Strategy %s not found", strategy_id)
            return None

        if not self._registry.is_enabled(strategy_id):
            LOG.debug("Strategy %s is disabled", strategy_id)
            return None

        return await self._calculate_one(
            strategy,
            prepared,
            semaphore=asyncio.Semaphore(self._strategy_concurrency),
        )

    async def _calculate_one(
        self,
        strategy: Any,  # AbstractStrategy
        prepared: PreparedSymbol,
        *,
        semaphore: asyncio.Semaphore,
    ) -> SignalResult:
        """Calculate signal from single strategy with timeout and error handling."""
        strategy_id = strategy.strategy_id
        symbol = prepared.symbol if prepared else "unknown"
        queued_at = time.perf_counter()

        async with semaphore:
            start_time = time.perf_counter()
            queue_wait_ms = (start_time - queued_at) * 1000.0
            if (
                self._max_queue_wait_seconds > 0.0
                and queue_wait_ms > self._max_queue_wait_seconds * 1000.0
            ):
                decision = StrategyDecision.skip(
                    setup_id=strategy_id,
                    reason_code="runtime.strategy_queue_stale",
                    details={
                        "symbol": symbol,
                        "queue_wait_ms": queue_wait_ms,
                        "max_queue_wait_seconds": self._max_queue_wait_seconds,
                    },
                )
                return SignalResult(
                    setup_id=strategy_id,
                    signal=None,
                    decision=decision,
                    calculation_time_ms=0.0,
                    metadata={
                        "setup_id": strategy_id,
                        "queue_wait_ms": queue_wait_ms,
                        "compute_ms": 0.0,
                    },
                )

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
                global _executor_timeout_count
                _executor_timeout_count += 1
                _executor_timeout_by_strategy[strategy_id] = (
                    _executor_timeout_by_strategy.get(strategy_id, 0) + 1
                )
                LOG.warning(
                    "strategy_timeout",
                    extra={"setup_id": strategy_id, "timeout_seconds": self._timeout},
                )
                if _executor_timeout_count % 10 == 0:
                    LOG.warning(
                        "strategy executor timeout count reached %d; latest timeout=%s latest_strategy_timeouts=%d",
                        _executor_timeout_count,
                        strategy_id,
                        _executor_timeout_by_strategy[strategy_id],
                    )
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
        required_features = list(getattr(metadata, "required_features", ()) or ())
        required_enrichment = list(getattr(metadata, "required_enrichment", ()) or ())
        missing_fields: list[str] = []
        details: dict[str, Any] = {
            "required_context": required_context,
            "required_features": required_features,
            "required_enrichment": required_enrichment,
        }
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
        elif getattr(metadata, "requires_oi", False) and not _has_oi_context(prepared):
            missing_fields.extend(("oi_current", "oi_change_pct"))
            reason_code = "data.oi_context_missing"
        elif getattr(metadata, "requires_funding", False) and prepared.funding_rate is None:
            missing_fields.append("funding_rate")
            reason_code = "data.funding_rate_missing"
        elif missing_feature_fields := _missing_required_features(prepared, required_features):
            missing_fields.extend(missing_feature_fields)
            reason_code = "data.required_features_missing"
        elif missing_enrichment_fields := _missing_required_enrichment(
            prepared, required_enrichment
        ):
            missing_fields.extend(missing_enrichment_fields)
            reason_code = "data.required_enrichment_missing"

        return StrategyDecision.skip(
            setup_id=strategy_id,
            reason_code=reason_code,
            details=details,
            missing_fields=tuple(sorted(set(missing_fields))),
        )

    def _strategy_is_active_for_symbol(self, strategy: Any, prepared: PreparedSymbol) -> bool:
        strategy_id = str(getattr(strategy, "setup_id", getattr(strategy, "strategy_id", "")) or "")
        if self._feature_flags is not None and hasattr(self._feature_flags, "is_strategy_enabled"):
            if not self._feature_flags.is_strategy_enabled(strategy_id):
                return False
        checker = getattr(strategy, "is_active_now", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(prepared, self._settings))
        except TypeError:
            try:
                return bool(checker(prepared))
            except Exception:
                LOG.exception(
                    "%s: strategy schedule check failed | strategy=%s",
                    prepared.symbol,
                    getattr(strategy, "strategy_id", "unknown"),
                )
                return True
        except Exception:
            LOG.exception(
                "%s: strategy schedule check failed | strategy=%s",
                prepared.symbol,
                getattr(strategy, "strategy_id", "unknown"),
            )
            return True

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
        valid_signals = [
            r.signal
            for r in results
            if r.is_valid
            and r.signal is not None
            and r.signal.score >= 0.38  # score floor: 0..1 confidence delivery minimum.
        ]

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
            "strategy_concurrency_per_symbol": self._strategy_concurrency,
        }
