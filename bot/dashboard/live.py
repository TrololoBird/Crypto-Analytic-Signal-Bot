"""Live dashboard data aggregation.

The dashboard must explain the current signal funnel even when no Telegram
messages were delivered. This module reads the running bot state plus the latest
telemetry JSONL files and returns bounded, JSON-serializable summaries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bot.dashboard._live_helpers import (
    _build_funnel_widget,
    _compute_cycle_totals,
    _compute_session_delta,
    _counter_rows,
    _cycle_delivered_count,
    _delivery_success_rows,
    _effective_shortlist,
    _frame_readiness_fields,
    _is_routing_excluded_decision_reason,
    _labeled_counter_rows,
    _parse_ts,
    _rejected_row_confirmation_profile,
    _rejected_row_confirmations,
    _safe_float,
    _safe_int,
    _unified_top_blocker,
)
from bot.diagnostics.facade import assess_radar_store
from bot.domain.labels import (
    CONFIRMATION_PROFILE_KEYS,
    CONFLUENCE_LEG_KEYS,
    confirmation_profile_label_ru,
    confluence_leg_label_ru,
    confluence_profile_recommendation_ru,
    normalize_reject_reason,
    reject_reason_ru,
)
from bot.market.radar_state import SymbolTier
from bot.runtime.errors import DEFENSIVE_EXC
from bot.runtime_policy import effective_shortlist_unified_routing
from bot.telemetry import slim_message_buffer_fields

from ..delivery.formatting import message_preview, sample_message_from_row

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

JsonDict = dict[str, Any]
LOG = logging.getLogger("bot.dashboard_live")
PRIORITY_ASSET_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)


@dataclass(frozen=True, slots=True)
class JsonlFileRef:
    """Reference to a telemetry JSONL file."""

    name: str
    path: Path
    run_id: str
    modified_at: float
    size: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _tracking_open_counts(bot: Any) -> dict[str, int]:
    """Sync read of pending/active rows for dashboard overview (via repo)."""
    repo = getattr(bot, "_modern_repo", None)
    if repo is not None and hasattr(repo, "get_open_signal_counts_sync"):
        try:
            return repo.get_open_signal_counts_sync()
        except (OSError, AttributeError):
            pass
    return {"pending": 0, "active": 0, "open": 0}


class DashboardLiveData:
    """Bounded telemetry reader for dashboard endpoints.

    Parameters
    ----------
    bot_getter:
        Callable returning the current ``SignalBot`` instance.
    cache_ttl_seconds:
        Short TTL used to avoid reparsing large JSONL files on every dashboard
        poll.
    """

    def __init__(
        self,
        bot_getter: Callable[[], Any],
        *,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._bot_getter = bot_getter
        self._cache_ttl = max(1.0, float(cache_ttl_seconds))
        self._cache: dict[tuple[str, tuple[Any, ...]], tuple[float, Any]] = {}

    async def overview(self) -> JsonDict:
        """Return a high-level live dashboard summary."""
        return await self._cached_async("overview", (), self._overview_uncached)

    async def funnel(self, *, max_rows: int = 100_000) -> JsonDict:
        """Return cycle, rejection, decision, and delivery funnel summary."""
        return await self._cached_async(
            "funnel", (max_rows,), lambda: self._funnel_uncached(max_rows=max_rows)
        )

    async def funnel_reconcile(self, *, max_rows: int = 100_000) -> JsonDict:
        """Compare cycle delivery_success totals vs delivery.jsonl success rows."""
        return await self._cached_async(
            "funnel_reconcile",
            (max_rows,),
            lambda: self._funnel_reconcile_uncached(max_rows=max_rows),
        )

    async def shortlist(self, *, limit: int = 80) -> JsonDict:
        """Return shortlist composition and last telemetry rows."""
        return await self._cached_async(
            "shortlist", (limit,), lambda: self._shortlist_uncached(limit=limit)
        )

    async def radar_summary(self, *, hot_limit: int = 25) -> JsonDict:
        """Return live radar store health and top HOT/DEEP symbols."""
        return await self._cached_async(
            "radar_summary",
            (hot_limit,),
            lambda: self._radar_summary_uncached(hot_limit=hot_limit),
        )

    async def rejections(self, *, limit: int = 30, max_rows: int = 100_000) -> JsonDict:
        """Return rejection reason and stage summaries."""
        return await self._cached_async(
            "rejections",
            (limit, max_rows),
            lambda: self._rejections_uncached(limit=limit, max_rows=max_rows),
        )

    async def confluence_legs(self, *, max_rows: int = 100_000) -> JsonDict:
        """Return hard confluence leg failure counts from rejected telemetry."""
        return await self._cached_async(
            "confluence_legs",
            (max_rows,),
            lambda: self._confluence_legs_uncached(max_rows=max_rows),
        )

    async def confluence_legs_by_profile(self, *, max_rows: int = 100_000) -> JsonDict:
        """Return confluence leg failures grouped by confirmation_profile."""
        return await self._cached_async(
            "confluence_legs_by_profile",
            (max_rows,),
            lambda: self._confluence_legs_by_profile_uncached(max_rows=max_rows),
        )

    async def decisions(self, *, limit: int = 40, max_rows: int = 100_000) -> JsonDict:
        """Return strategy-decision summaries."""
        return await self._cached_async(
            "decisions",
            (limit, max_rows),
            lambda: self._decisions_uncached(limit=limit, max_rows=max_rows),
        )

    async def runtime(self) -> JsonDict:
        """Return runtime health, data-quality, and websocket summaries."""
        return await self._cached_async("runtime", (), self._runtime_uncached)

    async def delivery(self, *, limit: int = 25) -> JsonDict:
        """Return delivery and selected-signal telemetry."""
        return await self._cached_async(
            "delivery", (limit,), lambda: self._delivery_uncached(limit=limit)
        )

    async def telegram_preview(self) -> JsonDict:
        """Return a Telegram-format preview from the freshest signal-like row."""
        return await self._cached_async("telegram_preview", (), self._telegram_preview_uncached)

    async def _cached_async(
        self, name: str, args: tuple[Any, ...], factory: Callable[[], Any]
    ) -> Any:
        key = (name, args)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] <= self._cache_ttl:
            return cached[1]
        result = factory()
        if asyncio.iscoroutine(result):
            result = await result
        self._cache[key] = (now, result)
        return result

    def _bot(self) -> Any:
        return self._bot_getter()

    def _settings(self) -> Any | None:
        return getattr(self._bot(), "settings", None)

    def _priority_symbols(self) -> list[str]:
        settings = self._settings()
        symbols = {symbol.upper() for symbol in PRIORITY_ASSET_SYMBOLS}
        universe = getattr(settings, "universe", None)
        if universe is not None:
            symbols.update(
                str(symbol).strip().upper()
                for symbol in getattr(universe, "pinned_symbols", ()) or ()
                if str(symbol).strip()
            )
        assets = getattr(settings, "assets", {}) or {}
        if isinstance(assets, dict):
            for symbol, config in assets.items():
                if bool(getattr(config, "deep_analysis", False)):
                    symbols.add(str(symbol).strip().upper())
        return sorted(symbols)

    def _telemetry_dir(self) -> Path | None:
        settings = self._settings()
        value = getattr(settings, "telemetry_dir", None)
        return Path(value) if value else None

    def _current_run_id(self) -> str | None:
        telemetry = getattr(self._bot(), "telemetry", None)
        run_id = getattr(telemetry, "run_id", None)
        return str(run_id) if run_id else None

    def _runs_dir(self) -> Path | None:
        telemetry_dir = self._telemetry_dir()
        if telemetry_dir is None:
            return None
        runs_dir = telemetry_dir / "runs"
        return runs_dir if runs_dir.exists() else None

    def _latest_run_id(self) -> str | None:
        runs_dir = self._runs_dir()
        if runs_dir is None:
            return None
        run_dirs = [path for path in runs_dir.iterdir() if path.is_dir()]
        if not run_dirs:
            return None
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return run_dirs[0].name

    def _preferred_run_id(self) -> str | None:
        return self._current_run_id() or self._latest_run_id()

    def _analysis_dir(self, run_id: str | None = None) -> Path | None:
        runs_dir = self._runs_dir()
        selected = run_id or self._preferred_run_id()
        if runs_dir is None or not selected:
            return None
        path = runs_dir / selected / "analysis"
        return path if path.exists() else None

    def _jsonl_refs(self, stem: str, *, limit_files: int = 2) -> list[JsonlFileRef]:
        runs_dir = self._runs_dir()
        if runs_dir is None:
            return []
        current = self._preferred_run_id()
        files = list(runs_dir.glob(f"*/analysis/{stem}*.jsonl"))
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        if current:
            preferred = [path for path in files if path.parts[-3] == current]
            rest = [path for path in files if path.parts[-3] != current]
            files = preferred + rest
        refs: list[JsonlFileRef] = []
        seen: set[Path] = set()
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            stat = path.stat()
            refs.append(
                JsonlFileRef(
                    name=path.name,
                    path=path,
                    run_id=path.parts[-3],
                    modified_at=stat.st_mtime,
                    size=stat.st_size,
                )
            )
            if len(refs) >= max(1, int(limit_files)):
                break
        return refs

    def _read_tail(self, ref: JsonlFileRef | None, *, limit: int) -> list[JsonDict]:
        if ref is None or limit <= 0:
            return []
        rows: list[JsonDict] = []
        try:
            with ref.path.open("r", encoding="utf-8", errors="ignore") as handle:
                tail = deque(handle, maxlen=max(1, int(limit)))
            for line in reversed(tail):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        except OSError:
            return []
        return rows

    def _iter_recent(
        self,
        stem: str,
        *,
        max_rows: int,
        limit_files: int = 2,
    ) -> Iterable[JsonDict]:
        remaining = max(1, int(max_rows))
        for ref in self._jsonl_refs(stem, limit_files=limit_files):
            if remaining <= 0:
                break
            rows = self._read_tail(ref, limit=remaining)
            remaining -= len(rows)
            yield from rows

    def _overview_uncached(self) -> JsonDict:
        bot = self._bot()
        run_id = self._preferred_run_id()
        tracking = _tracking_open_counts(bot)
        shortlist = _effective_shortlist(bot)
        status = {
            "run_id": run_id,
            "generated_at": _utc_now().isoformat(),
            "running": not getattr(getattr(bot, "_shutdown", None), "is_set", lambda: True)(),
            "shortlist_size": len(shortlist),
            "shortlist_source": getattr(bot, "_shortlist_source", "unknown"),
            "open_signals": tracking["open"],
            "pending_signals": tracking["pending"],
            "active_signals": tracking["active"],
            "delivery_provider": str(
                getattr(getattr(getattr(bot, "settings", None), "notifiers", None), "provider", "")
                or "unknown"
            ),
        }
        last_cycles = list(self._iter_recent("cycles", max_rows=20, limit_files=1))
        latest_cycle = last_cycles[0] if last_cycles else {}
        status.update(
            {
                "last_cycle": latest_cycle,
                "last_cycle_detector_runs": _safe_int(latest_cycle.get("detector_runs")),
                "last_cycle_candidates": _safe_int(latest_cycle.get("candidate_count")),
                "last_cycle_delivered": _cycle_delivered_count(latest_cycle),
                "last_cycle_shortlist": _safe_int(latest_cycle.get("shortlist_size")),
            }
        )
        decisions = self._decisions_uncached(limit=10, max_rows=30_000)
        rejections = self._rejections_uncached(limit=10, max_rows=30_000)
        status["decision_signal_rate"] = decisions.get("signal_rate", 0.0)
        status["decision_rows"] = decisions.get("total_rows", 0)
        cycles = list(self._iter_recent("cycles", max_rows=500, limit_files=1))
        cycle_totals = _compute_cycle_totals(cycles)
        session_delta = _compute_session_delta(cycles)
        cycle_totals["session_delta"] = session_delta
        status["funnel_widget"] = _build_funnel_widget(cycle_totals, session_delta)
        status["cycle_totals"] = cycle_totals
        delivery = self._delivery_uncached(limit=10)
        status["session_delivered"] = _safe_int(
            delivery.get("delivery_success_count", delivery.get("delivery_count"))
        )
        rejected_counter: Counter[str] = Counter()
        for row in rejections.get("reasons", []):
            if isinstance(row, dict) and row.get("key"):
                rejected_counter[str(row["key"])] += _safe_int(row.get("count"))
        decision_counter: Counter[str] = Counter()
        for row in self._iter_recent("strategy_decisions", max_rows=30_000, limit_files=2):
            if str(row.get("status") or "").strip().lower() == "signal":
                continue
            reason_code = str(row.get("reason_code") or row.get("reason") or "unknown")
            decision_counter[normalize_reject_reason(reason_code)] += 1
        top_blocker = _unified_top_blocker(
            rejected_counter=rejected_counter,
            decision_counter=decision_counter,
        )
        status["top_blocker"] = top_blocker
        status["top_rejection"] = top_blocker or (
            rejections.get("reasons", [{}])[0] if rejections.get("reasons") else {}
        )
        runtime_rows = list(self._iter_recent("health_runtime", max_rows=5, limit_files=1))
        status["runtime_health"] = runtime_rows[0] if runtime_rows else {}
        status.update(_frame_readiness_fields(bot))
        status.update(self._market_state_fields())
        return status

    def _market_state_fields(self) -> JsonDict:
        bot = self._bot()
        regime = getattr(getattr(bot, "market_regime", None), "_last_result", None)
        if regime is None:
            return {
                "market_state": {"available": False},
                "btc_bias": "neutral",
                "eth_bias": "neutral",
                "market_regime": "unknown",
                "market_strength": 0.0,
                "btc_phase": "unknown",
                "volatility_regime": "unknown",
                "risk_on_off": "neutral",
            }
        payload = cast("dict[str, Any]", regime.to_dict())
        payload["available"] = True
        updater = getattr(bot, "_market_context_updater", None)
        display = getattr(updater, "_last_display_snapshot", None) if updater else None
        if isinstance(display, dict) and display:
            payload["display"] = display
        return {
            "market_state": payload,
            "btc_bias": payload.get("btc_bias", "neutral"),
            "eth_bias": payload.get("eth_bias", "neutral"),
            "market_regime": payload.get("regime", "unknown"),
            "market_strength": payload.get("strength", 0.0),
            "btc_phase": payload.get("btc_phase", "unknown"),
            "volatility_regime": payload.get("volatility_regime", "unknown"),
            "risk_on_off": payload.get("risk_on_off", "neutral"),
            "funding_sentiment": payload.get("funding_sentiment", "neutral"),
            "oi_momentum": payload.get("oi_momentum", "stable"),
            "altcoin_season_index": payload.get("altcoin_season_index"),
        }

    def _funnel_uncached(self, *, max_rows: int) -> JsonDict:
        cycles = list(self._iter_recent("cycles", max_rows=500, limit_files=1))
        cycle_totals = _compute_cycle_totals(cycles)
        session_delta = _compute_session_delta(cycles)
        cycle_totals["session_delta"] = session_delta
        funnel_widget = _build_funnel_widget(cycle_totals, session_delta)
        decisions = self._decisions_uncached(limit=50, max_rows=max_rows)
        rejects = self._rejections_uncached(limit=25, max_rows=max_rows)
        delivery = self._delivery_uncached(limit=10)
        rejected_counter: Counter[str] = Counter()
        for row in rejects.get("reasons", []):
            if isinstance(row, dict) and row.get("key"):
                rejected_counter[str(row["key"])] += _safe_int(row.get("count"))
        decision_reject_counter: Counter[str] = Counter()
        for row in self._iter_recent("strategy_decisions", max_rows=max_rows, limit_files=2):
            if str(row.get("status") or "").strip().lower() == "signal":
                continue
            reason_code = str(row.get("reason_code") or row.get("reason") or "unknown")
            decision_reject_counter[normalize_reject_reason(reason_code)] += 1
        decision_rejects = _labeled_counter_rows(decision_reject_counter, limit=10)
        top_blocker = _unified_top_blocker(
            rejected_counter=rejected_counter,
            decision_counter=decision_reject_counter,
        )
        combined_hint: JsonDict | None = None
        if top_blocker:
            combined_hint = {
                "source": "+".join(top_blocker.get("sources") or ["merged"]),
                "key": top_blocker.get("key"),
                "count": top_blocker.get("count"),
                "label_ru": top_blocker.get("label_ru"),
            }
        return {
            "run_id": self._preferred_run_id(),
            "generated_at": _utc_now().isoformat(),
            "cycle_totals": cycle_totals,
            "funnel_widget": funnel_widget,
            "top_blocker": top_blocker,
            "latest_cycles": cycles[:12],
            "decisions": decisions,
            "rejections": rejects,
            "decision_rejects": {
                "total_rows": sum(decision_reject_counter.values()),
                "reasons": decision_rejects,
            },
            "combined_reject_hint": combined_hint,
            "delivery": delivery,
            "efficiency": {
                "raw_signal_rate": decisions.get("signal_rate", 0.0),
                "candidate_rate_per_cycle": round(
                    cycle_totals["candidates"] / max(cycle_totals["cycles"], 1),
                    4,
                ),
                "delivery_rate_per_cycle": round(
                    cycle_totals["delivered"] / max(cycle_totals["cycles"], 1),
                    4,
                ),
            },
        }

    def _radar_summary_uncached(self, *, hot_limit: int) -> JsonDict:
        bot = self._bot()
        ws_mgr = getattr(bot, "_ws_manager", None)
        store = getattr(ws_mgr, "_radar_store", None) if ws_mgr is not None else None
        cfg = getattr(getattr(bot, "settings", None), "universe", None)
        radar_cfg = getattr(cfg, "radar", None) if cfg is not None else None
        health = assess_radar_store(store, config=radar_cfg or object())
        hot_rows: list[JsonDict] = []
        if store is not None and hasattr(store, "symbols_by_tier"):
            for tier in (SymbolTier.DEEP, SymbolTier.HOT):
                for symbol in store.symbols_by_tier(tier)[:hot_limit]:
                    state = store._states.get(symbol) if hasattr(store, "_states") else None
                    hot_rows.append(
                        {
                            "symbol": symbol,
                            "tier": tier.value,
                            "prescore_boost": getattr(state, "prescore_boost", None),
                            "flags": list(getattr(state, "flags", ()) or ())[:5],
                            "promotion_reasons": list(
                                getattr(state, "promotion_reasons", ()) or ()
                            )[:4],
                        }
                    )
        shortlist_radar = 0
        for item in list(getattr(bot, "_shortlist", []) or []):
            if getattr(item, "shortlist_bucket", "") == "radar" or any(
                "radar" in str(r) for r in (getattr(item, "shortlist_reasons", ()) or ())
            ):
                shortlist_radar += 1
        return {
            "generated_at": _utc_now().isoformat(),
            "health": health,
            "shortlist_radar_promoted": shortlist_radar,
            "top_symbols": hot_rows[:hot_limit],
        }

    def _shortlist_uncached(self, *, limit: int) -> JsonDict:
        bot = self._bot()
        pinned = (
            {
                str(symbol).strip().upper()
                for symbol in getattr(
                    getattr(bot, "settings", None), "universe", object()
                ).pinned_symbols
            }
            if getattr(getattr(bot, "settings", None), "universe", None) is not None
            else set()
        )
        priority_symbols = set(self._priority_symbols())
        items = []
        for item in list(_effective_shortlist(bot))[: max(1, int(limit))]:
            symbol = str(getattr(item, "symbol", ""))
            reasons = list(getattr(item, "shortlist_reasons", ()) or ())[:6]
            items.append(
                {
                    "symbol": symbol,
                    "bucket": getattr(item, "shortlist_bucket", None),
                    "source": getattr(item, "seed_source", None),
                    "score": getattr(item, "shortlist_score", None),
                    "quote_volume": getattr(item, "quote_volume", None),
                    "price_change_pct": getattr(item, "price_change_pct", None),
                    "strategy_fit_count": len(getattr(item, "strategy_fits", ()) or ()),
                    "strategy_fits": list(getattr(item, "strategy_fits", ()) or ())[:12],
                    "shortlist_reasons": reasons,
                    "radar_promoted": getattr(item, "shortlist_bucket", "") == "radar"
                    or any("radar" in str(r) for r in reasons),
                    "pinned": symbol.upper() in pinned,
                    "priority": symbol.upper() in priority_symbols,
                }
            )
        telemetry_rows = list(self._iter_recent("shortlist", max_rows=50, limit_files=1))
        build_rows = list(self._iter_recent("shortlist_build", max_rows=5, limit_files=1))
        latest = telemetry_rows[0] if telemetry_rows else {}
        latest_build = build_rows[0] if build_rows else {}
        fit_counts = [row["strategy_fit_count"] for row in items]
        item_by_symbol = {str(item["symbol"]).upper(): item for item in items}
        telemetry_symbols = {
            str(symbol).upper() for symbol in (latest.get("symbols") or []) if str(symbol).strip()
        }
        priority_activity = self._priority_activity(priority_symbols)
        priority_rows = []
        for rank, symbol in enumerate(sorted(priority_symbols), start=1):
            item = item_by_symbol.get(symbol)
            priority_rows.append(
                {
                    "symbol": symbol,
                    "rank": rank,
                    "in_memory": item is not None,
                    "in_latest_telemetry": symbol in telemetry_symbols,
                    "score": item.get("score") if item else None,
                    "bucket": item.get("bucket") if item else None,
                    "source": item.get("source") if item else None,
                    "strategy_fit_count": item.get("strategy_fit_count") if item else 0,
                    **priority_activity.get(symbol, {}),
                }
            )
        return {
            "run_id": self._preferred_run_id(),
            "source": getattr(bot, "_shortlist_source", latest.get("source", "unknown")),
            "total": len(items),
            "pinned": sum(1 for item in items if item["pinned"]),
            "priority_total": len(priority_rows),
            "priority_in_memory": sum(1 for item in priority_rows if item["in_memory"]),
            "priority_in_telemetry": sum(
                1 for item in priority_rows if item["in_latest_telemetry"]
            ),
            "priority_missing": [
                item["symbol"] for item in priority_rows if not item["in_latest_telemetry"]
            ],
            "dynamic": sum(1 for item in items if not item["pinned"]),
            "zero_fit": sum(1 for count in fit_counts if count == 0),
            "avg_fit": round(sum(fit_counts) / max(len(fit_counts), 1), 2),
            "gate_passed": latest.get("gate_passed"),
            "light_pool": latest.get("light_pool"),
            "light_pool_limit": latest.get("light_pool_limit"),
            "latest_telemetry": latest,
            "telemetry_tail": telemetry_rows[:10],
            "radar_tier_cycle": latest_build.get("radar_tier_cycle"),
            "radar": latest_build.get("radar"),
            "radar_promoted_count": sum(1 for row in items if row.get("radar_promoted")),
            "priority_assets": priority_rows,
            "items": items,
        }

    def _priority_activity(self, symbols: set[str]) -> dict[str, JsonDict]:
        if not symbols:
            return {}
        activity: dict[str, JsonDict] = {
            symbol: {
                "decision_rows": 0,
                "candidate_rows": 0,
                "selected_rows": 0,
                "delivery_rows": 0,
                "rejected_rows": 0,
                "top_rejection": None,
            }
            for symbol in symbols
        }
        rejection_reasons: dict[str, Counter[str]] = {symbol: Counter() for symbol in symbols}
        for stem, key, max_rows in (
            ("strategy_decisions", "decision_rows", 50_000),
            ("candidates", "candidate_rows", 20_000),
            ("selected", "selected_rows", 10_000),
            ("delivery", "delivery_rows", 10_000),
            ("rejected", "rejected_rows", 50_000),
        ):
            for row in self._iter_recent(stem, max_rows=max_rows, limit_files=2):
                symbol = str(row.get("symbol") or "").strip().upper()
                if symbol not in activity:
                    continue
                activity[symbol][key] = int(activity[symbol].get(key, 0) or 0) + 1
                if stem == "rejected":
                    reason = normalize_reject_reason(str(row.get("reason") or "unknown"))
                    rejection_reasons[symbol][reason] += 1
        for symbol, counter in rejection_reasons.items():
            if counter:
                reason, count = counter.most_common(1)[0]
                activity[symbol]["top_rejection"] = {
                    "reason": reason,
                    "count": count,
                    "label_ru": reject_reason_ru(reason),
                }
        return activity

    def _rejections_uncached(self, *, limit: int, max_rows: int) -> JsonDict:
        reasons: Counter[str] = Counter()
        stages: Counter[str] = Counter()
        setups: Counter[str] = Counter()
        symbols: Counter[str] = Counter()
        examples: dict[str, JsonDict] = {}
        total = 0
        for row in self._iter_recent("rejected", max_rows=max_rows, limit_files=2):
            total += 1
            raw_reason = str(row.get("reason") or "unknown")
            reason = normalize_reject_reason(raw_reason)
            reasons[reason] += 1
            stages[str(row.get("stage") or "unknown")] += 1
            setups[str(row.get("setup_id") or "unknown")] += 1
            symbols[str(row.get("symbol") or "unknown")] += 1
            if reason not in examples:
                examples[reason] = {**row, "raw_reason": raw_reason}
        return {
            "run_id": self._preferred_run_id(),
            "total_rows": total,
            "reasons": [
                {
                    **item,
                    "label_ru": reject_reason_ru(str(item["key"])),
                    "example": examples.get(str(item["key"]), {}),
                    "pct": round(int(item["count"]) / max(total, 1) * 100.0, 2),
                }
                for item in _counter_rows(reasons, limit=limit)
            ],
            "stages": _counter_rows(stages, limit=limit),
            "setups": _counter_rows(setups, limit=limit),
            "symbols": _counter_rows(symbols, limit=limit),
        }

    def _confluence_legs_uncached(self, *, max_rows: int) -> JsonDict:
        leg_failures: Counter[str] = Counter()
        gate_rejects = 0
        for row in self._iter_recent("rejected", max_rows=max_rows, limit_files=2):
            confirmations = _rejected_row_confirmations(row)
            if confirmations is None:
                continue
            gate_rejects += 1
            for leg in CONFLUENCE_LEG_KEYS:
                if leg in confirmations and not confirmations[leg]:
                    leg_failures[leg] += 1
        legs = [
            {
                "key": leg,
                "count": int(leg_failures.get(leg, 0)),
                "label_ru": confluence_leg_label_ru(leg),
            }
            for leg in CONFLUENCE_LEG_KEYS
        ]
        return {
            "run_id": self._preferred_run_id(),
            "generated_at": _utc_now().isoformat(),
            "gate_rejects": gate_rejects,
            "leg_failures": legs,
            "total_leg_failures": sum(leg_failures.values()),
        }

    def _confluence_legs_by_profile_uncached(self, *, max_rows: int) -> JsonDict:
        profile_gate_rejects: Counter[str] = Counter()
        profile_leg_failures: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self._iter_recent("rejected", max_rows=max_rows, limit_files=2):
            confirmations = _rejected_row_confirmations(row)
            if confirmations is None:
                continue
            profile = _rejected_row_confirmation_profile(row)
            profile_gate_rejects[profile] += 1
            for leg in CONFLUENCE_LEG_KEYS:
                if leg in confirmations and not confirmations[leg]:
                    profile_leg_failures[profile][leg] += 1

        active_profiles = sorted(
            profile_gate_rejects.keys(),
            key=lambda key: (-profile_gate_rejects[key], key),
        )
        profile_rows = []
        for profile in active_profiles:
            leg_failures = profile_leg_failures[profile]
            top_leg = ""
            top_leg_count = 0
            if leg_failures:
                top_leg, top_leg_count = leg_failures.most_common(1)[0]
            profile_rows.append(
                {
                    "key": profile,
                    "label_ru": confirmation_profile_label_ru(profile),
                    "gate_rejects": int(profile_gate_rejects[profile]),
                    "leg_failures": [
                        {
                            "key": leg,
                            "count": int(leg_failures.get(leg, 0)),
                            "label_ru": confluence_leg_label_ru(leg),
                        }
                        for leg in CONFLUENCE_LEG_KEYS
                    ],
                    "total_leg_failures": sum(leg_failures.values()),
                    "top_failing_leg": top_leg or None,
                    "recommendation": confluence_profile_recommendation_ru(
                        profile,
                        top_leg=top_leg,
                        leg_count=top_leg_count,
                    ),
                }
            )

        return {
            "run_id": self._preferred_run_id(),
            "generated_at": _utc_now().isoformat(),
            "gate_rejects": sum(profile_gate_rejects.values()),
            "profiles": profile_rows,
            "known_profiles": [
                {
                    "key": profile,
                    "label_ru": confirmation_profile_label_ru(profile),
                }
                for profile in CONFIRMATION_PROFILE_KEYS
            ],
        }

    def _decisions_uncached(self, *, limit: int, max_rows: int) -> JsonDict:
        status: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        families: Counter[str] = Counter()
        setup_status: dict[str, Counter[str]] = defaultdict(Counter)
        setup_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        total = 0
        routed_total = 0
        for row in self._iter_recent("strategy_decisions", max_rows=max_rows, limit_files=2):
            total += 1
            setup = str(row.get("setup_id") or row.get("strategy_id") or "unknown")
            state = str(row.get("status") or "unknown")
            reason = str(row.get("reason_code") or row.get("reason") or "unknown")
            family = str(row.get("reason_family") or reason.split(".", 1)[0])
            status[state] += 1
            reasons[reason] += 1
            families[family] += 1
            setup_status[setup][state] += 1
            if not _is_routing_excluded_decision_reason(reason):
                routed_total += 1
            if state != "signal":
                setup_reasons[setup][reason] += 1
        setup_rows = []
        for setup, counts in setup_status.items():
            runs = sum(counts.values())
            signals = counts.get("signal", 0)
            setup_rows.append(
                {
                    "setup_id": setup,
                    "total": runs,
                    "signals": signals,
                    "rejects": counts.get("reject", 0),
                    "skips": counts.get("skip", 0),
                    "signal_rate": round(signals / max(runs, 1), 6),
                    "top_blockers": _counter_rows(setup_reasons[setup], limit=5),
                }
            )
        setup_rows.sort(key=lambda row: (-row["total"], row["signal_rate"], row["setup_id"]))
        signals_total = status.get("signal", 0)
        return {
            "run_id": self._preferred_run_id(),
            "total_rows": total,
            "status_counts": dict(status),
            "reason_counts": _counter_rows(reasons, limit=limit),
            "reason_family_counts": _counter_rows(families, limit=limit),
            "setup_reports": setup_rows[: max(1, int(limit))],
            "zero_signal_setups": [
                row for row in setup_rows if row["total"] > 0 and row["signals"] == 0
            ],
            "signal_rate": round(signals_total / max(total, 1), 6),
            "routed_signal_rate": round(signals_total / max(routed_total, 1), 6),
            "routed_total_rows": routed_total,
        }

    def _runtime_uncached(self) -> JsonDict:
        health = list(self._iter_recent("health", max_rows=20, limit_files=1))
        health_runtime = list(self._iter_recent("health_runtime", max_rows=20, limit_files=1))
        data_quality = list(self._iter_recent("data_quality", max_rows=50, limit_files=1))
        fallback = list(self._iter_recent("fallback_checks", max_rows=50, limit_files=1))
        public_intel = list(self._iter_recent("public_intelligence", max_rows=20, limit_files=1))
        bot = self._bot()
        ws_snapshot = {}
        ws = getattr(bot, "_ws_manager", None)
        if ws is not None and hasattr(ws, "state_snapshot"):
            try:
                ws_snapshot = ws.state_snapshot()
            except DEFENSIVE_EXC as exc:
                LOG.debug("dashboard live ws snapshot unavailable: %s", exc)
                ws_snapshot = {}
        diagnostics = getattr(bot, "_signal_diagnostics", None)
        diag_summary = diagnostics.get_summary() if diagnostics is not None else {}
        runtime_cfg = getattr(getattr(bot, "settings", None), "runtime", None)
        shortlist_total = len(getattr(bot, "_shortlist", ()) or ())
        effective_unified = effective_shortlist_unified_routing(
            runtime_cfg,
            shortlist_total=shortlist_total,
        )
        quality_monitor = getattr(bot, "quality_monitor", None)
        quality_payload = (
            quality_monitor.telemetry_snapshot()
            if quality_monitor is not None and hasattr(quality_monitor, "telemetry_snapshot")
            else {}
        )
        telemetry_mismatch = self._telemetry_mismatch_uncached()
        buffer_fields = slim_message_buffer_fields(
            ws_snapshot if isinstance(ws_snapshot, dict) else {}
        )
        rest_weight: dict[str, Any] = {}
        proxy_pool: dict[str, Any] = {}
        client = getattr(bot, "client", None) or getattr(bot, "_market_data", None)
        if client is not None:
            snap_fn = getattr(client, "state_snapshot", None)
            if callable(snap_fn):
                with contextlib.suppress(DEFENSIVE_EXC):
                    snap = dict(snap_fn())
                    used = float(snap.get("rest_weight_1m") or 0.0)
                    alert_pct = float(getattr(runtime_cfg, "dashboard_weight_alert_pct", 80.0))
                    soft = 1800.0
                    rest_weight = {
                        "used_weight_1m": used,
                        "utilization_pct": round(used / soft * 100.0, 2) if used else 0.0,
                        "alert": used >= soft * alert_pct / 100.0,
                        **snap,
                    }
            pool_fn = getattr(client, "proxy_pool_snapshot", None)
            if callable(pool_fn):
                with contextlib.suppress(DEFENSIVE_EXC):
                    proxy_pool = dict(pool_fn())
        ws_broadcaster = getattr(getattr(bot, "dashboard", None), "_ws_broadcaster", None)
        ws_dropped = int(getattr(ws_broadcaster, "dropped_count", 0) or 0)
        return {
            "run_id": self._preferred_run_id(),
            "shortlist_unified_routing": bool(
                getattr(runtime_cfg, "shortlist_unified_routing", False)
            ),
            "effective_shortlist_unified_routing": effective_unified,
            "shortlist_total": shortlist_total,
            "enable_strategy_lanes": bool(getattr(runtime_cfg, "enable_strategy_lanes", True)),
            "quality_monitor": quality_payload.get("quality_monitor", {}),
            "latest_health": health[0] if health else {},
            "latest_runtime": health_runtime[0] if health_runtime else {},
            "latest_data_quality": data_quality[0] if data_quality else {},
            "latest_fallback": fallback[0] if fallback else {},
            "latest_public_intelligence": public_intel[0] if public_intel else {},
            "ws_snapshot": ws_snapshot,
            **buffer_fields,
            "signal_diagnostics": diag_summary,
            "telemetry_mismatch": telemetry_mismatch,
            "health_tail": health[:8],
            "data_quality_tail": data_quality[:8],
            "rest_weight": rest_weight,
            "proxy_pool": proxy_pool,
            "dashboard_ws_dropped": ws_dropped,
        }

    def _funnel_reconcile_uncached(self, *, max_rows: int) -> JsonDict:
        cycles = list(self._iter_recent("cycles", max_rows=max_rows, limit_files=3))
        delivery = list(self._iter_recent("delivery", max_rows=max_rows, limit_files=3))
        cycles_delivered = sum(_cycle_delivered_count(row) for row in cycles)
        delivery_success = len(_delivery_success_rows(delivery))
        delta = cycles_delivered - delivery_success
        return {
            "run_id": self._preferred_run_id(),
            "cycles_rows": len(cycles),
            "delivery_rows": len(delivery),
            "cycles_delivery_success_total": cycles_delivered,
            "delivery_jsonl_success_total": delivery_success,
            "delta": delta,
            "match": delta == 0,
        }

    def _telemetry_mismatch_uncached(self, *, max_rows: int = 10_000) -> JsonDict:
        refs = self._jsonl_refs("telemetry_mismatch", limit_files=2)
        counts: Counter[str] = Counter()
        total = 0
        for row in self._iter_recent("telemetry_mismatch", max_rows=max_rows, limit_files=2):
            total += 1
            counts[str(row.get("mismatch_type") or "unknown")] += 1
        return {
            "available": bool(refs) or total > 0,
            "total_rows": total,
            "counts": [{"key": key, "count": int(count)} for key, count in counts.most_common(20)],
        }

    def _delivery_uncached(self, *, limit: int) -> JsonDict:
        selected = list(self._iter_recent("selected", max_rows=max(50, limit * 4), limit_files=2))
        delivery = list(self._iter_recent("delivery", max_rows=max(50, limit * 4), limit_files=2))
        delivery_success = _delivery_success_rows(delivery)
        rows = [{**row, "source": "delivery"} for row in delivery_success[:limit]]
        if not rows:
            rows = [
                {**row, "source": "selected", "delivery_status": "selected"}
                for row in selected[:limit]
            ]
        return {
            "run_id": self._preferred_run_id(),
            "selected_count": len(selected),
            "delivery_count": len(delivery),
            "delivery_success_count": len(delivery_success),
            "delivery_status_counts": dict(
                Counter(
                    str(row.get("delivery_status") or row.get("status") or "unknown")
                    for row in delivery
                )
            ),
            "rows": rows,
        }

    def _telegram_preview_uncached(self) -> JsonDict:
        row = self._freshest_signal_like_row()
        if not row:
            return {
                "available": False,
                "reason": "no_signal_like_telemetry",
                "preview": {},
                "html": "",
            }
        html_text = sample_message_from_row(row)
        return {
            "available": True,
            "source_row": row,
            "preview": message_preview(html_text),
            "html": html_text,
        }

    def _freshest_signal_like_row(self) -> JsonDict | None:
        for stem in ("selected", "delivery", "candidates"):
            rows = list(self._iter_recent(stem, max_rows=50, limit_files=2))
            for row in rows:
                if row.get("symbol") and row.get("setup_id") and self._row_has_signal_plan(row):
                    return row
        for row in self._iter_recent("strategy_decisions", max_rows=500, limit_files=2):
            if row.get("status") == "signal" and row.get("symbol") and row.get("setup_id"):
                if not self._row_has_signal_plan(row):
                    continue
                return {
                    "symbol": row.get("symbol"),
                    "setup_id": row.get("setup_id"),
                    "direction": row.get("direction", "long"),
                    "score": row.get("score") or row.get("signal_score") or 0.0,
                    "timeframe": row.get("timeframe", "15m"),
                    "tracking_ref": "preview",
                    "entry_low": row.get("entry_low") or row.get("entry_price") or 0.0,
                    "entry_high": row.get("entry_high") or row.get("entry_price") or 0.0,
                    "stop": row.get("stop") or row.get("stop_price") or 0.0,
                    "take_profit_1": row.get("take_profit_1") or row.get("tp1") or 0.0,
                    "take_profit_2": row.get("take_profit_2") or row.get("tp2") or 0.0,
                    "created_at": row.get("ts") or _utc_now().isoformat(),
                    "reasons": [row.get("reason") or "pattern.raw_hit"],
                }
        return None

    def _row_has_signal_plan(self, row: Mapping[str, Any]) -> bool:
        entry = (
            row.get("entry_low")
            or row.get("entry_high")
            or row.get("entry_price")
            or row.get("entry")
        )
        stop = row.get("stop") or row.get("stop_price") or row.get("stop_loss")
        target = (
            row.get("take_profit_1")
            or row.get("tp1")
            or row.get("target_1")
            or row.get("take_profit")
        )
        return _safe_float(entry) > 0 and _safe_float(stop) > 0 and _safe_float(target) > 0

    def latest_file_catalog(self) -> JsonDict:
        """Return latest telemetry file metadata for diagnostics."""
        stems = [
            "cycles",
            "shortlist",
            "rejected",
            "strategy_decisions",
            "selected",
            "delivery",
            "telemetry_mismatch",
            "health",
            "health_runtime",
            "data_quality",
        ]
        catalog: dict[str, list[JsonDict]] = {}
        for stem in stems:
            catalog[stem] = [
                {
                    "name": ref.name,
                    "run_id": ref.run_id,
                    "path": str(ref.path),
                    "size": ref.size,
                    "modified_at": datetime.fromtimestamp(ref.modified_at, tz=UTC).isoformat(),
                }
                for ref in self._jsonl_refs(stem, limit_files=3)
            ]
        return {
            "preferred_run_id": self._preferred_run_id(),
            "current_run_id": self._current_run_id(),
            "latest_run_id": self._latest_run_id(),
            "files": catalog,
        }


def funnel_stage_counts(funnel: Mapping[str, Any]) -> JsonDict:
    """Compact stage counts for dashboard WS ``funnel_update`` payloads."""
    totals = funnel.get("cycle_totals") if isinstance(funnel.get("cycle_totals"), dict) else {}
    decisions = funnel.get("decisions") if isinstance(funnel.get("decisions"), dict) else {}
    status_counts = (
        decisions.get("status_counts") if isinstance(decisions.get("status_counts"), dict) else {}
    )
    return {
        "detected": _safe_int(totals.get("detector_runs")),
        "merged": _safe_int(totals.get("candidates")),
        "confluence": _safe_int(status_counts.get("signal")),
        "tier": _safe_int(totals.get("selected")),
        "delivered": _safe_int(totals.get("delivered")),
    }


def funnel_stage_counts_from_cycle(
    *,
    cycle_row: Mapping[str, Any],
    funnel: Mapping[str, Any] | None = None,
) -> JsonDict:
    """Per-cycle stage counts derived from telemetry cycle rows."""
    nested = funnel if isinstance(funnel, dict) else {}
    if not nested and isinstance(cycle_row.get("funnel"), dict):
        nested = cycle_row["funnel"]
    candidates = _safe_int(
        nested.get("post_filter_candidates", cycle_row.get("candidate_count")),
    )
    selected = _safe_int(
        cycle_row.get("selected_count", cycle_row.get("selected_signals")),
    )
    rejects_by_stage = nested.get("rejects_by_stage")
    confluence_rejects = 0
    if isinstance(rejects_by_stage, dict):
        confluence_rejects = _safe_int(rejects_by_stage.get("confluence"))
    confluence = max(0, candidates - confluence_rejects)
    return {
        "detected": _safe_int(nested.get("raw_hits", cycle_row.get("detector_runs"))),
        "merged": candidates,
        "confluence": confluence,
        "tier": selected,
        "delivered": _cycle_delivered_count(cycle_row),
    }


def summarize_rows_by_symbol(
    rows: Iterable[Mapping[str, Any]], *, limit: int = 20
) -> list[JsonDict]:
    """Summarize arbitrary telemetry rows by symbol."""
    counter: Counter[str] = Counter()
    setups: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        symbol = str(row.get("symbol") or "unknown")
        setup = str(row.get("setup_id") or row.get("strategy_id") or "unknown")
        counter[symbol] += 1
        setups[symbol][setup] += 1
    out = []
    for symbol, count in counter.most_common(limit):
        out.append(
            {
                "symbol": symbol,
                "count": int(count),
                "top_setups": _counter_rows(setups[symbol], limit=5),
            }
        )
    return out


def summarize_timeseries(rows: Iterable[Mapping[str, Any]], *, field: str) -> list[JsonDict]:
    """Bucket telemetry rows by minute for dashboard sparklines."""
    buckets: dict[str, float] = defaultdict(float)
    for row in rows:
        ts = _parse_ts(row.get("ts") or row.get("created_at") or row.get("timestamp"))
        if ts is None:
            continue
        key = ts.strftime("%H:%M")
        buckets[key] += _safe_float(row.get(field), 1.0)
    return [{"bucket": key, "value": value} for key, value in sorted(buckets.items())]


def classify_dashboard_health(overview: Mapping[str, Any]) -> JsonDict:
    """Classify dashboard health from overview metrics."""
    issues: list[str] = []
    shortlist_size = _safe_int(overview.get("shortlist_size"))
    detector_rows = _safe_int(overview.get("decision_rows"))
    signal_rate = _safe_float(overview.get("decision_signal_rate"))
    if shortlist_size < 30:
        issues.append("shortlist_below_expected")
    if detector_rows <= 0:
        issues.append("no_strategy_decision_rows")
    if detector_rows > 0 and signal_rate <= 0.0:
        issues.append("zero_raw_signal_rate")
    return {
        "status": "healthy" if not issues else "degraded",
        "issues": issues,
        "checked_at": _utc_now().isoformat(),
    }
