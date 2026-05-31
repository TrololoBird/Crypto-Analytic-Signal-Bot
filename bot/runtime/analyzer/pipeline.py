from __future__ import annotations

from bot.runtime.analyzer.common import *  # noqa: F403
from bot.runtime.analyzer.common import (
    _apply_setup_score_adjustment,
    _attach_rejection_rollups,
)


class AnalyzerPipelineMixin:
    async def run_modern_analysis(
        self,
        item: UniverseSymbol,
        frames: SymbolFrames,
        trigger: str = "modern_engine",
        event_ts: datetime | None = None,
        ws_enrichments: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Run modern SignalEngine analysis for a symbol.

        Replaces legacy SignalPipeline.process_symbol().

        Returns:
            PipelineResult compatible with legacy pipeline output
        """
        event_ts = event_ts or datetime.now(UTC)
        candidates: list[Signal] = []
        rejected: list[dict[str, Any]] = []
        prepared: PreparedSymbol | None = None
        funnel: dict[str, Any] = {
            "shortlist_entered": True,
            "frame_rows": {},
            "frame_readiness": {},
            "detector_runs": 0,
            "post_filter_candidates": 0,
            "raw_hits": 0,
            "raw_hits_by_setup": {},
            "strategy_rejects_by_setup": {},
            "family_precheck_rejects": 0,
            "alignment_penalties": 0,
            "confirmation_rejects": 0,
            "filters_rejects": 0,
            "selected": 0,
            "delivered": 0,
        }

        LOG.info("%s: starting modern analysis | trigger=%s", item.symbol, trigger)
        diagnostics = getattr(self._bot, "_signal_diagnostics", None)
        if diagnostics is not None:
            diagnostics.record_symbol_analyzed(item.symbol)
        item = self._bot._refresh_universe_symbol_from_ws(item)
        if not item.strategy_fits:
            LOG.warning(
                "%s: strategy_fits is EMPTY - routing bypassed and all enabled strategies "
                "will run. shortlist_score=%.4f bucket=%s source=%s",
                item.symbol,
                item.shortlist_score or 0.0,
                item.shortlist_bucket,
                item.seed_source,
            )
        else:
            LOG.debug(
                "%s: strategy_fits=%d %s",
                item.symbol,
                len(item.strategy_fits),
                list(item.strategy_fits)[:5],
            )

        minimums = self._minimums()
        rows_4h = frames.df_4h.height if frames.df_4h is not None else 0
        rows_5m = frames.df_5m.height if frames.df_5m is not None else 0
        rows_1h = frames.df_1h.height
        rows_15m = frames.df_15m.height
        funnel["frame_rows"] = {
            "15m": rows_15m,
            "1h": rows_1h,
            "5m": rows_5m,
            "4h": rows_4h,
        }
        funnel["frame_readiness"] = {
            "15m": rows_15m >= minimums["15m"],
            "1h": rows_1h >= minimums["1h"],
            "5m": rows_5m >= minimums["5m"],
            "4h": rows_4h >= minimums["4h"],
        }
        if (
            rows_5m < minimums["5m"]
            or rows_15m < minimums["15m"]
            or rows_1h < minimums["1h"]
            or rows_4h < minimums["4h"]
        ):
            missing_required = []
            if rows_5m < minimums["5m"]:
                missing_required.append("5m")
            if rows_15m < minimums["15m"]:
                missing_required.append("15m")
            if rows_1h < minimums["1h"]:
                missing_required.append("1h")
            if rows_4h < minimums["4h"]:
                missing_required.append("4h")
            rejected.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": item.symbol,
                    "setup_id": "data",
                    "direction": "none",
                    "stage": "data",
                    "reason": "insufficient_required_history",
                    "rows_1h": rows_1h,
                    "rows_15m": rows_15m,
                    "rows_5m": rows_5m,
                    "rows_4h": rows_4h,
                    "need_1h": minimums["1h"],
                    "need_15m": minimums["15m"],
                    "need_5m": minimums["5m"],
                    "need_4h": minimums["4h"],
                    "missing_required_frames": missing_required,
                }
            )
            LOG.info(
                "%s: insufficient required history for analysis | 5m=%d/%d 15m=%d/%d 1h=%d/%d 4h=%d/%d",
                item.symbol,
                rows_5m,
                minimums["5m"],
                rows_15m,
                minimums["15m"],
                rows_1h,
                minimums["1h"],
                rows_4h,
                minimums["4h"],
            )
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                status="insufficient_required_history",
                prepared=None,
                funnel=funnel,
            )

        try:
            # Build prepared symbol using modern prepare_symbol
            prepared = await asyncio.to_thread(
                prepare_symbol,
                item,
                frames,
                minimums=minimums,
                settings=self._bot.settings,
                ws_manager=self._bot._ws_manager,
            )
            LOG.debug(
                "%s: prepared symbol built | work_15m_rows=%s work_1h_rows=%s",
                item.symbol,
                prepared.work_15m.height
                if prepared is not None and prepared.work_15m is not None
                else 0,
                prepared.work_1h.height
                if prepared is not None and prepared.work_1h is not None
                else 0,
            )
        except Exception as exc:
            self._bot._prepare_error_count += 1
            error_payload = build_runtime_error_payload(
                component="symbol_analyzer.prepare_symbol",
                exc=exc,
                symbol=item.symbol,
                extra={"stage": "prepare_symbol", "ts": datetime.now(UTC).isoformat()},
            )
            self._bot._last_prepare_error = error_payload
            funnel["prepare_error_stage"] = "prepare_symbol"
            funnel["prepare_error_exception_type"] = type(exc).__name__
            funnel["prepare_error_class"] = error_payload["error_class"]
            LOG.exception("%s: failed to build prepared symbol", item.symbol)
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                error=str(exc),
                status="prepare_error",
                prepared=prepared,
                funnel=funnel,
            )

        if prepared is not None and ws_enrichments:
            try:
                for key, value in ws_enrichments.items():
                    if hasattr(prepared, key):
                        setattr(prepared, key, value)
                # Debug: log enrichment status
                if ws_enrichments.get("mark_index_spread_bps") is not None:
                    LOG.debug(
                        "%s: enrichment mark_index_spread_bps=%.4f",
                        item.symbol,
                        ws_enrichments["mark_index_spread_bps"],
                    )
                else:
                    LOG.debug(
                        "%s: enrichment mark_index_spread_bps=None (ws_data_missing)",
                        item.symbol,
                    )
            except _DEGRADATION_ERRORS as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="ws_enrichment_apply",
                    source="ws_cache",
                    reason=str(exc),
                    fallback_used="skip_ws_enrichment",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="ws_enrichment_apply",
                    source="ws_cache",
                    reason=str(exc),
                    fallback_used="skip_ws_enrichment",
                    exception_type=type(exc).__name__,
                )

        if prepared is not None:
            try:
                market_ctx = await self._bot._modern_repo.get_market_context()
                for key in (
                    "btc_bias",
                    "eth_bias",
                    "sol_bias",
                    "xau_bias",
                    "xag_bias",
                    "pax_bias",
                    "altcoin_season_index",
                    "btc_phase",
                    "macro_risk_mode",
                    "benchmark_context",
                ):
                    value = market_ctx.get(key)
                    if value is not None and hasattr(prepared, key):
                        setattr(prepared, key, value)
                benchmark_context = market_ctx.get("benchmark_context")
                if isinstance(benchmark_context, dict):
                    for symbol, attr in (
                        ("SOLUSDT", "sol_bias"),
                        ("XAUUSDT", "xau_bias"),
                        ("XAGUSDT", "xag_bias"),
                        ("PAXGUSDT", "pax_bias"),
                    ):
                        payload = benchmark_context.get(symbol)
                        if isinstance(payload, dict):
                            bias = payload.get("bias")
                            if bias:
                                setattr(prepared, attr, str(bias))
            except _DEGRADATION_ERRORS as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="market_context",
                    source="memory",
                    reason=str(exc),
                    fallback_used="skip_multi_asset_context",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="market_context",
                    source="memory",
                    reason=str(exc),
                    fallback_used="skip_multi_asset_context",
                    exception_type=type(exc).__name__,
                )

        # Run modern engine (replaces pipeline analysis)
        if prepared is None:
            LOG.info("%s: prepared symbol is None", item.symbol)
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                status="prepare_failed",
                prepared=None,
                funnel=funnel,
            )

        # Log engine stats before calculation
        engine_stats = self._bot._modern_engine.get_engine_stats()
        LOG.debug(
            "%s: engine stats | enabled_strategies=%d total=%d",
            item.symbol,
            engine_stats.get("enabled_strategies", 0),
            engine_stats.get("total_strategies", 0),
        )
        self._bot._diagnostic_trace_counts[item.symbol] = 0

        try:
            signal_results = await self._bot._modern_engine.calculate_all(prepared)
            funnel["detector_runs"] = len(signal_results)
            LOG.debug(
                "%s: engine calculated | results_count=%d",
                item.symbol,
                len(signal_results),
            )
        except Exception as exc:
            error_class = classify_runtime_error(exc)
            funnel["engine_error_class"] = error_class
            LOG.exception(
                "%s: modern engine calculation failed | error_class=%s",
                item.symbol,
                error_class,
            )
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                error=str(exc),
                status="engine_error",
                prepared=prepared,
                funnel=funnel,
            )

        # Process results: convert SignalResult to Signal, then apply the
        # production hard-gate + confluence path before a signal can become a
        # runtime candidate.
        signals_found = 0
        signals_rejected_perf = 0
        signals_added = 0

        for result in signal_results:
            setup_id = (
                result.setup_id
                or result.metadata.get("setup_id")
                or getattr(result.signal, "setup_id", "unknown")
            )
            setup_id = str(setup_id)
            if diagnostics is not None:
                diagnostics.record_detector_run(setup_id)
            decision = result.decision
            if decision is None:
                decision = StrategyDecision.error_result(
                    setup_id=setup_id,
                    reason_code="runtime.missing_decision",
                    error=result.error or "missing strategy decision",
                    stage="engine",
                    details={"symbol": item.symbol},
                )
            self._bot._append_strategy_decision_telemetry(
                symbol=item.symbol,
                trigger=trigger,
                decision=decision,
            )
            if decision.is_error or decision.is_skip or decision.is_reject:
                funnel["strategy_rejects_by_setup"][setup_id] = (
                    funnel["strategy_rejects_by_setup"].get(setup_id, 0) + 1
                )
                rejected.append(
                    self._bot._decision_to_reject_row(symbol=item.symbol, decision=decision)
                )
                LOG.debug(
                    "%s: strategy produced no signal | setup=%s status=%s reason=%s",
                    item.symbol,
                    setup_id,
                    decision.status,
                    decision.reason_code,
                )
                continue

            signal = decision.signal or result.signal
            if signal is None:
                fallback_decision = StrategyDecision.reject(
                    setup_id=setup_id,
                    stage="strategy",
                    reason_code="runtime.signal_missing_after_hit",
                    details={"symbol": item.symbol},
                )
                funnel["strategy_rejects_by_setup"][setup_id] = (
                    funnel["strategy_rejects_by_setup"].get(setup_id, 0) + 1
                )
                rejected.append(
                    self._bot._decision_to_reject_row(
                        symbol=item.symbol, decision=fallback_decision
                    )
                )
                continue

            setup_id = signal.setup_id
            metadata = self._bot._strategy_metadata(setup_id)
            signal = self._bot._apply_strategy_metadata(signal, metadata)

            precheck_ok, precheck_reason, precheck_details = self.check_family_precheck(
                signal,
                prepared,
                metadata,
            )
            if not precheck_ok:
                rejected.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "family_precheck",
                        "reason": precheck_reason or "family_precheck_reject",
                        "details": precheck_details,
                    }
                )
                funnel["family_precheck_rejects"] += 1
                continue
            if precheck_details.get("soft_penalty_applied"):
                penalty_factor = float(precheck_details.get("penalty_factor", 1.0))
                reason = str(precheck_details.get("penalty_reason") or "family_precheck_penalty")
                signal = replace(
                    signal,
                    score=round(max(signal.score * penalty_factor, 0.0), 4),
                    reasons=signal.reasons
                    if reason in signal.reasons
                    else (*signal.reasons, reason),
                )

            signal, alignment_details = self.apply_alignment_penalty(signal, prepared, metadata)
            if alignment_details.get("applied"):
                funnel["alignment_penalties"] += 1

            signals_found += 1
            funnel["raw_hits"] += 1
            funnel["raw_hits_by_setup"][signal.setup_id] = (
                funnel["raw_hits_by_setup"].get(signal.setup_id, 0) + 1
            )
            if diagnostics is not None:
                diagnostics.record_detector_hit(signal.setup_id)

            ltf_ok, ltf_reason, ltf_details = self.check_family_confirmation(
                signal, prepared, metadata
            )
            if not ltf_ok:
                if diagnostics is not None:
                    diagnostics.record_confirmation_reject(
                        signal.setup_id,
                        ltf_reason or "5m_confirmation_reject",
                    )
                rejected.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "confirmation",
                        "reason": ltf_reason or "5m_confirmation_reject",
                        "details": ltf_details,
                    }
                )
                funnel["confirmation_rejects"] += 1
                continue
            if ltf_details.get("fast_context_weak"):
                signal = replace(
                    signal,
                    score=round(max(signal.score * 0.95, 0.0), 4),
                    reasons=signal.reasons
                    if "fast_context_weak" in signal.reasons
                    else (*signal.reasons, "fast_context_weak"),
                )

            # Apply adaptive setup scoring using modern repo. A -0.05 penalty is
            # calibration input, not enough evidence to suppress every signal.
            score_adj = await self._bot._modern_repo.get_setup_score_adjustment(signal.setup_id)
            signal, perf_details = _apply_setup_score_adjustment(signal, score_adj)
            if perf_details.get("applied"):
                funnel["performance_adjustments"] = funnel.get("performance_adjustments", 0) + 1
                self._bot._append_symbol_trace(
                    symbol=item.symbol,
                    row={
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "stage": "performance_adjustment",
                        "details": perf_details,
                    },
                )

            filter_result = apply_global_filters(
                signal,
                prepared,
                self._bot.settings,
                self._bot.confluence,
            )
            if filter_result is None:
                passed = False
                filtered_signal = signal
                filter_reason = "filter_pipeline_crash"
                scoring_result = None
                filter_details = None
            else:
                passed, filtered_signal, filter_reason, scoring_result, filter_details = filter_result
            if not passed:
                LOG.info(
                    "%s: signal filtered | setup=%s dir=%s score=%.3f reason=%s",
                    item.symbol,
                    signal.setup_id,
                    signal.direction,
                    signal.score,
                    filter_reason,
                )
                if diagnostics is not None:
                    reason = filter_reason or "filter_rejected"
                    diagnostics.record_filter_reject(signal.setup_id, reason)
                    if reason.startswith("stale_"):
                        diagnostics.record_stale_symbol(item.symbol)
                reject_row: dict[str, Any] = {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": item.symbol,
                    "setup_id": signal.setup_id,
                    "direction": signal.direction,
                    "stage": "filters",
                    "reason": filter_reason or "filter_rejected",
                }
                if scoring_result is not None:
                    scoring_payload = scoring_result.to_dict()
                    scoring_payload["setup_id"] = signal.setup_id
                    reject_row["scoring"] = scoring_payload
                if filter_details:
                    reject_row["details"] = filter_details
                rejected.append(reject_row)
                funnel["filters_rejects"] += 1
                continue

            candidates.append(filtered_signal)
            if diagnostics is not None:
                diagnostics.record_candidate(filtered_signal.setup_id)
            signals_added += 1
            LOG.debug(
                "%s: candidate signal | setup=%s dir=%s score=%.3f rr=%.2f",
                item.symbol,
                filtered_signal.setup_id,
                filtered_signal.direction,
                filtered_signal.score,
                filtered_signal.risk_reward or 0,
            )

        LOG.info(
            "%s: analysis complete | trigger=%s raw_strategies=%d signals_found=%d perf_rejected=%d candidates=%d",
            item.symbol,
            trigger,
            len(signal_results),
            signals_found,
            signals_rejected_perf,
            signals_added,
        )
        funnel["post_filter_candidates"] = len(candidates)
        if diagnostics is not None and not signal_results:
            diagnostics.record_zero_detector_symbol(item.symbol)
        _attach_rejection_rollups(funnel, rejected)

        return PipelineResult(
            symbol=item.symbol,
            trigger=trigger,
            event_ts=event_ts,
            raw_setups=len(signal_results),
            candidates=candidates,
            rejected=rejected,
            status="no_setups" if len(signal_results) == 0 else "ok",
            prepared=prepared,
            funnel=funnel,
        )

