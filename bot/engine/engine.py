"""Signal engine for orchestrating strategy calculations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bot.runtime.errors import DEFENSIVE_EXC, classify_runtime_error

from ..diagnostics.signals import get_global_diagnostics
from ..domain.strategies import StrategyDecision
from ..market.data_capability import assess_strategy_data_capability
from ..market.fit import asset_fit_reject_reason, market_context_from_prepared
from ..market.strategy_pools import DATA_POOL_SETUPS
from ..runtime_policy import effective_engine_score_floor
from .base import SignalResult
from .lanes import is_standard_kline_interval, select_lane_setups

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal
    from .registry import StrategyRegistry

LOG = logging.getLogger("bot.engine.engine")


def _default_executor_workers() -> int:
    cpus = os.cpu_count() or 4
    return max(2, min(16, cpus * 2))


_STRATEGY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_default_executor_workers(),
    thread_name_prefix="signal-strategy",
)


@dataclass
class _ExecutorModuleState:
    warmed_workers: int = 0
    timeout_count: int = 0
    timeout_by_strategy: dict[str, int] = field(default_factory=dict)


_EXECUTOR_MODULE_STATE = _ExecutorModuleState()


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

    @staticmethod
    def _record_routing_skip(setup_id: str, reason: str) -> None:
        diagnostics = get_global_diagnostics()
        if diagnostics is not None:
            diagnostics.record_routing_skip(setup_id, reason)

    def _route_strategies(
        self,
        prepared: PreparedSymbol,
        *,
        event_interval: str | None,
    ) -> tuple[list[Any], list[SignalResult]]:
        """Select enabled strategies for this symbol/event (fits, lanes, route_all)."""
        symbol = prepared.symbol if prepared else "unknown"
        all_enabled = self._registry.get_enabled()
        routing_skips: list[SignalResult] = []
        runtime = self._settings.runtime
        universe = getattr(prepared, "universe", None)
        strategy_fits = set(getattr(universe, "strategy_fits", ()) or ())
        shortlist_score = getattr(universe, "shortlist_score", None)
        is_shortlist_asset = shortlist_score is not None
        pinned_symbols = {
            str(item).strip().upper()
            for item in getattr(self._settings.universe, "pinned_symbols", ())
        }
        is_pinned_symbol = symbol.upper() in pinned_symbols
        route_all = bool(runtime.route_all_enabled_strategies)
        enable_lanes = bool(runtime.enable_strategy_lanes)
        emit_routing_skips = bool(runtime.emit_strategy_routing_skips)
        shortlist_unified_routing = bool(getattr(runtime, "shortlist_unified_routing", True))
        use_unified_shortlist_routing = shortlist_unified_routing and (
            is_shortlist_asset or is_pinned_symbol
        )

        if not strategy_fits and not is_pinned_symbol:
            LOG.warning(
                "%s: strategy_fits is EMPTY - routing coverage is degraded for this symbol. "
                "Check _strategy_fits_for_row() in universe.py",
                symbol,
            )

        if enable_lanes and event_interval and not route_all:
            apply_interval = is_standard_kline_interval(event_interval)
            use_fits_filter = not use_unified_shortlist_routing
            fits_arg = tuple(strategy_fits) if strategy_fits and use_fits_filter else None
            priority_setup_ids: tuple[str, ...] | None = None
            if use_unified_shortlist_routing:
                pool_priority = (
                    DATA_POOL_SETUPS.get("orderflow", frozenset())
                    | DATA_POOL_SETUPS.get("positioning", frozenset())
                    | DATA_POOL_SETUPS.get("orderbook", frozenset())
                    | DATA_POOL_SETUPS.get("multi_asset", frozenset())
                )
                priority_setup_ids = tuple(sorted(pool_priority))
            lane_metas = select_lane_setups(
                self._registry,
                symbol=symbol,
                interval=event_interval if apply_interval else "",
                settings=self._settings,
                strategy_fits=fits_arg,
                apply_interval_filter=apply_interval,
                priority_setup_ids=priority_setup_ids,
            )
            lane_ids = {meta.strategy_id for meta in lane_metas}
            strategies = [strategy for strategy in all_enabled if strategy.strategy_id in lane_ids]
            if emit_routing_skips:
                for strategy in all_enabled:
                    if strategy.strategy_id in lane_ids:
                        continue
                    decision = StrategyDecision.skip(
                        setup_id=strategy.strategy_id,
                        reason_code="runtime.strategy_lane_excluded",
                        details={
                            "symbol": symbol,
                            "event_interval": event_interval,
                            "lane_setup_count": len(lane_ids),
                            "lane_family_count": len({m.family for m in lane_metas}),
                        },
                    )
                    routing_skips.append(
                        SignalResult(
                            setup_id=strategy.strategy_id,
                            signal=None,
                            decision=decision,
                            metadata={
                                "setup_id": strategy.strategy_id,
                                "reason": decision.reason_code,
                                "event_interval": event_interval,
                            },
                            calculation_time_ms=0.0,
                        )
                    )
                    self._record_routing_skip(strategy.strategy_id, decision.reason_code)
            LOG.info(
                "%s: lane routing | interval=%s families=%d setups=%d",
                symbol,
                event_interval,
                len({meta.family for meta in lane_metas}),
                len(strategies),
            )
            return strategies, routing_skips

        strategies = list(all_enabled)
        if route_all and (is_shortlist_asset or is_pinned_symbol):
            LOG.debug(
                "%s: shortlist routing expanded to all enabled strategies | strategy_fits=%d",
                symbol,
                len(strategy_fits),
            )
        elif strategy_fits and not route_all and not use_unified_shortlist_routing:
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
                    self._record_routing_skip(strategy.strategy_id, decision.reason_code)
            strategies = routed
        return strategies, routing_skips

    def _apply_detector_limits(
        self,
        strategies: list[Any],
        *,
        max_setups: int | None,
        setup_subset: frozenset[str] | None,
    ) -> list[Any]:
        limited = strategies
        if setup_subset:
            limited = [strategy for strategy in limited if strategy.strategy_id in setup_subset]
        if max_setups is not None and max_setups > 0:
            limited = limited[:max_setups]
        return limited

    async def calculate_all(
        self,
        prepared: PreparedSymbol,
        *,
        event_interval: str | None = None,
        max_setups: int | None = None,
        setup_subset: frozenset[str] | None = None,
    ) -> list[SignalResult]:
        """Calculate signals for strategies routed to this symbol/event.

        Args:
            prepared: Prepared symbol data
            event_interval: Kline (or cycle) interval for lane selection (target spec)

        Returns:
            List of SignalResult from routed strategies
        """
        symbol = prepared.symbol if prepared else "unknown"
        strategies, routing_skips = self._route_strategies(
            prepared,
            event_interval=event_interval,
        )
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
                        "schedule_checker": getattr(strategy, "setup_id", strategy.strategy_id),
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
            LOG.info(
                "%s: strategy schedule skipped | skipped=%d setups=%s",
                symbol,
                len(schedule_skip_results),
                [item.setup_id for item in schedule_skip_results[:5]],
            )
            routing_skips.extend(schedule_skip_results)
            for item in schedule_skip_results:
                reason = item.decision.reason_code if item.decision else "unknown"
                self._record_routing_skip(item.setup_id, reason)
        data_capable: list[Any] = []
        for strategy in scheduled:
            cap = assess_strategy_data_capability(strategy.strategy_id, prepared)
            if cap.ready:
                data_capable.append(strategy)
                continue
            decision = StrategyDecision.skip(
                setup_id=strategy.strategy_id,
                reason_code=cap.reason or "data.capability_not_ready",
                details={
                    "symbol": symbol,
                    "pool": cap.pool,
                    "strategy_id": strategy.strategy_id,
                },
            )
            routing_skips.append(
                SignalResult(
                    setup_id=strategy.strategy_id,
                    signal=None,
                    decision=decision,
                    metadata={
                        "setup_id": strategy.strategy_id,
                        "reason": decision.reason_code,
                        "data_pool": cap.pool,
                    },
                    calculation_time_ms=0.0,
                )
            )
            self._record_routing_skip(strategy.strategy_id, decision.reason_code)
        strategies = data_capable
        strategies = self._apply_detector_limits(
            strategies,
            max_setups=max_setups,
            setup_subset=setup_subset,
        )
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
                LOG.exception(
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
                    hit=result.signal is not None,
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

                return cast("SignalResult", result)

            except TimeoutError:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                _EXECUTOR_MODULE_STATE.timeout_count += 1
                _EXECUTOR_MODULE_STATE.timeout_by_strategy[strategy_id] = (
                    _EXECUTOR_MODULE_STATE.timeout_by_strategy.get(strategy_id, 0) + 1
                )
                LOG.warning(
                    "strategy_timeout",
                    extra={"setup_id": strategy_id, "timeout_seconds": self._timeout},
                )
                if _EXECUTOR_MODULE_STATE.timeout_count % 10 == 0:
                    LOG.warning(
                        (
                            "strategy executor timeout count reached %d; latest timeout=%s "
                            "latest_strategy_timeouts=%d"
                        ),
                        _EXECUTOR_MODULE_STATE.timeout_count,
                        strategy_id,
                        _EXECUTOR_MODULE_STATE.timeout_by_strategy[strategy_id],
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
                LOG.exception("Strategy %s failed | error_class=%s", strategy_id, error_class)
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
                details["asset_fit"] = cast("Callable[[], dict[str, Any]]", to_dict)()
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
        if (
            self._feature_flags is not None
            and hasattr(self._feature_flags, "is_strategy_enabled")
            and not self._feature_flags.is_strategy_enabled(strategy_id)
        ):
            return False
        checker = getattr(strategy, "is_active_now", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(prepared, self._settings))
        except TypeError:
            try:
                return bool(checker(prepared))
            except DEFENSIVE_EXC:
                LOG.exception(
                    "%s: strategy schedule check failed | strategy=%s",
                    prepared.symbol,
                    getattr(strategy, "strategy_id", "unknown"),
                )
                return False
        except DEFENSIVE_EXC:
            LOG.exception(
                "%s: strategy schedule check failed | strategy=%s",
                prepared.symbol,
                getattr(strategy, "strategy_id", "unknown"),
            )
            return False

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
        if self._executor_warmed or worker_count <= 0:
            return
        async with self._executor_warm_lock:
            if self._executor_warmed:
                return
            if worker_count <= _EXECUTOR_MODULE_STATE.warmed_workers:
                self._executor_warmed = True
                return
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *[
                    loop.run_in_executor(_STRATEGY_EXECUTOR, _executor_noop)
                    for _ in range(worker_count - _EXECUTOR_MODULE_STATE.warmed_workers)
                ]
            )
            _EXECUTOR_MODULE_STATE.warmed_workers = max(
                _EXECUTOR_MODULE_STATE.warmed_workers, worker_count
            )
            self._executor_warmed = True

    def close(self) -> None:
        return None

    def _engine_score_floor(self, prepared: PreparedSymbol | None = None) -> float:
        """Minimum score for get_best_signal - config-driven, not hardcoded."""
        return float(effective_engine_score_floor(self._settings, prepared_or_symbol=prepared))

    def get_best_signal(
        self,
        results: list[SignalResult],
        *,
        prepared: PreparedSymbol | None = None,
    ) -> Signal | None:
        """Select best signal from multiple results based on score.

        Args:
            results: List of SignalResult from strategies

        Returns:
            Best Signal or None if no valid signals
        """
        score_floor = self._engine_score_floor(prepared)
        valid_signals = [
            r.signal
            for r in results
            if r.is_valid and r.signal is not None and r.signal.score >= score_floor
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
        signals = [
            result.signal
            for result in results
            if result.is_valid and result.signal is not None and result.signal.score >= min_score
        ]

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
