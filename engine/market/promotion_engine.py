"""Promotion engine: radar tiers → shortlist merge and WS deep symbol set."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..domain.config import _ALL_SETUP_IDS, BotSettings
from ..domain.schemas import UniverseSymbol
from .radar_state import MarketRadarStore, SymbolTier
from .universe_screener import apply_screener_to_store

if TYPE_CHECKING:
    from ..domain.schemas import SymbolMeta

LOG = logging.getLogger("bot.market.promotion")

JsonDict = dict[str, Any]


class PromotionEngine:
    """Run tier cycles and merge radar-promoted symbols into production shortlist."""

    def __init__(self, settings: BotSettings) -> None:
        self._settings = settings
        self._cfg = settings.universe.radar

    def run_tier_cycle(
        self, store: MarketRadarStore, *, now: float | None = None
    ) -> dict[str, Any]:
        if not self._cfg.enabled:
            return {"enabled": False}
        ts = float(now if now is not None else time.monotonic())
        apply_screener_to_store(store, now=ts)
        pinned = {str(s).strip().upper() for s in self._settings.universe.pinned_symbols}
        hot_candidates: list[tuple[float, str]] = []
        warm_candidates: list[tuple[float, str]] = []

        for symbol, state in store._states.items():
            if symbol in pinned:
                state.tier = SymbolTier.DEEP
                state.prescore_boost = max(state.prescore_boost, self._cfg.prescore_boost_hot)
                continue
            score = state.prescore_boost
            if state.flags:
                score += min(abs(state.price_change_pct_24h) / 20.0, 0.15)
            vol_z = store.volume_zscore(symbol)
            score += min(max(vol_z, 0.0) * 0.04, 0.12)
            if state.tier in {SymbolTier.HOT, SymbolTier.DEEP}:
                hot_candidates.append((score, symbol))
            elif state.flags or state.tier == SymbolTier.WARM:
                warm_candidates.append((score, symbol))

        hot_candidates.sort(reverse=True)
        warm_candidates.sort(reverse=True)
        hot_limit = int(self._cfg.hot_pool_limit)
        warm_limit = int(self._cfg.warm_pool_limit)

        hot_set = {sym for _, sym in hot_candidates[:hot_limit]}
        warm_set = {sym for _, sym in warm_candidates[:warm_limit]}

        demoted = 0
        promoted = 0
        for symbol, state in store._states.items():
            if symbol in pinned:
                continue
            idle = ts - max(state.last_trigger_ts, state.promoted_at, state.last_update_ts)
            if symbol in hot_set:
                if state.tier != SymbolTier.HOT and state.tier != SymbolTier.DEEP:
                    if ts - state.last_trigger_ts >= self._cfg.promotion_cooldown_seconds:
                        state.tier = SymbolTier.HOT
                        state.promoted_at = ts
                        state.last_trigger_ts = ts
                        promoted += 1
                else:
                    state.tier = SymbolTier.HOT
                continue
            if symbol in warm_set:
                if state.tier == SymbolTier.COLD:
                    state.tier = SymbolTier.WARM
                    promoted += 1
                elif state.tier == SymbolTier.WARM:
                    pass
                continue
            if (
                state.tier in {SymbolTier.HOT, SymbolTier.WARM}
                and idle >= self._cfg.demotion_idle_seconds
            ):
                state.tier = SymbolTier.COLD
                state.prescore_boost = 0.0
                demoted += 1

        deep_symbols = self.select_deep_symbols(store)
        for symbol in deep_symbols:
            st = store._states.get(symbol)
            if st is not None:
                st.tier = SymbolTier.DEEP

        summary = {
            "enabled": True,
            "tiers": store.snapshot_summary(),
            "hot_pool": len(hot_set),
            "warm_pool": len(warm_set),
            "deep_symbols": len(deep_symbols),
            "promoted": promoted,
            "demoted": demoted,
        }
        store._last_tier_cycle_ts = ts
        return summary

    def select_deep_symbols(self, store: MarketRadarStore) -> frozenset[str]:
        pinned = {str(s).strip().upper() for s in self._settings.universe.pinned_symbols}
        if not self._cfg.enabled:
            return frozenset(pinned)
        ranked: list[tuple[float, str]] = []
        for symbol, state in store._states.items():
            if symbol in pinned:
                continue
            if state.tier not in {SymbolTier.HOT, SymbolTier.DEEP}:
                continue
            ranked.append(
                (state.prescore_boost + min(abs(state.price_change_pct_24h) / 30.0, 0.2), symbol)
            )
        ranked.sort(reverse=True)
        reserve = int(self._cfg.promotion_slots_reserve)
        extra = [sym for _, sym in ranked[:reserve]]
        deep_tier = set(store.symbols_by_tier(SymbolTier.DEEP))
        return frozenset(pinned | set(extra) | deep_tier)

    def enrich_ticker_rows(self, rows: list[JsonDict], store: MarketRadarStore) -> list[JsonDict]:
        if not self._cfg.enabled:
            return rows
        out: list[JsonDict] = []
        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            state = store.get(symbol)
            if state is None:
                out.append(row)
                continue
            if state.prescore_boost > 0.0:
                row["radar_prescore_boost"] = state.prescore_boost
            if state.flags:
                row["radar_flags"] = ",".join(state.flags)
            if state.promotion_reasons:
                row["radar_reasons"] = "|".join(state.promotion_reasons)
            row["radar_tier"] = state.tier.value
            out.append(row)
        return out

    def merge_shortlist(
        self,
        shortlist: list[UniverseSymbol],
        store: MarketRadarStore,
        *,
        meta_by_symbol: dict[str, SymbolMeta],
        seed_source: str,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        if not self._cfg.enabled:
            return shortlist, {"radar_merge": False}
        deep = self.select_deep_symbols(store)
        existing = {item.symbol for item in shortlist}
        merged = list(shortlist)
        added: list[str] = []

        for symbol in sorted(deep):
            if symbol in existing:
                for index, item in enumerate(merged):
                    if item.symbol == symbol:
                        merged[index] = replace(
                            item,
                            shortlist_reasons=tuple(
                                dict.fromkeys((*item.shortlist_reasons, "radar_deep"))
                            ),
                        )
                continue
            meta = meta_by_symbol.get(symbol)
            if meta is None:
                continue
            state = store.get(symbol)
            merged.append(
                UniverseSymbol(
                    symbol=symbol,
                    base_asset=str(meta.base_asset),
                    quote_asset=str(meta.quote_asset),
                    contract_type=str(getattr(meta, "contract_type", "") or "PERPETUAL"),
                    status=str(getattr(meta, "status", "") or "TRADING"),
                    onboard_date_ms=int(getattr(meta, "onboard_date_ms", 0) or 0),
                    quote_volume=float(state.quote_volume if state else 0.0),
                    price_change_pct=float(state.price_change_pct_24h if state else 0.0),
                    last_price=float(state.last_price if state else 0.0),
                    shortlist_bucket="radar",
                    shortlist_score=float(state.prescore_boost if state else 0.5),
                    shortlist_reasons=tuple(
                        state.promotion_reasons if state else ("radar_promoted",)
                    ),
                    seed_source=seed_source,
                    strategy_fits=tuple(_ALL_SETUP_IDS),
                )
            )
            added.append(symbol)
            existing.add(symbol)

        limit = int(self._settings.universe.shortlist_limit)
        if len(merged) > limit + len(self._settings.universe.pinned_symbols):
            pinned_set = set(self._settings.universe.pinned_symbols)
            protected = [
                item for item in merged if item.symbol in pinned_set or item.symbol in deep
            ]
            rest = [item for item in merged if item.symbol not in {p.symbol for p in protected}]
            rest.sort(
                key=lambda item: (item.shortlist_score or 0.0, item.quote_volume), reverse=True
            )
            slots = max(limit, len(protected))
            merged = protected + rest[: max(0, slots - len(protected))]

        return merged, {
            "radar_merge": True,
            "radar_deep_count": len(deep),
            "radar_added": added,
            "radar_tiers": store.snapshot_summary(),
        }
