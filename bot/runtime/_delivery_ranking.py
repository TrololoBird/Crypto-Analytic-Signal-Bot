"""Delivery ranking and portfolio cap helpers (extracted from delivery_orchestrator)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.delivery.contract import validate_signal_contract
from bot.delivery.tiers import rank_key as tier_rank_key
from bot.delivery.trade_plan import evaluate_publish_readiness
from bot.domain.limit_entry import resolve_late_entry_chase_pct
from bot.domain.strategy_catalog import catalog_setup_family
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.domain.schemas import PreparedSymbol, Signal
    from bot.runtime.bot import SignalBot


LOG = logging.getLogger("bot.runtime.bot")


class DeliveryRankingMixin:
    """Setup-first candidate ranking with portfolio caps."""

    _bot: SignalBot

    def _rank_key(self, signal: Signal, *, diversity_state: dict[str, Any] | None = None) -> tuple:
        base = tier_rank_key(signal)
        if not diversity_state:
            return base
        symbol_penalty = 0.04 * int(
            diversity_state.get("symbol_counts", {}).get(signal.symbol.upper(), 0)
        )
        direction_penalty = 0.03 * int(
            diversity_state.get("direction_counts", {}).get(str(signal.direction or "").lower(), 0)
        )
        cooldown_penalty = 0.0
        cooldown_ages = diversity_state.get("cooldown_age_minutes", {})
        if isinstance(cooldown_ages, dict):
            age_min = cooldown_ages.get(signal.tracking_id)
            if age_min is not None:
                cooldown_minutes = float(
                    getattr(self._bot.settings.filters, "cooldown_minutes", 0) or 0
                )
                if cooldown_minutes > 0:
                    proximity = max(0.0, 1.0 - float(age_min) / cooldown_minutes)
                    cooldown_penalty = 0.06 * proximity
        return (
            base[0] - symbol_penalty - direction_penalty - cooldown_penalty,
            base[1],
            base[2],
        )

    @staticmethod
    def _symbol_direction_cooldown_key(signal: Signal) -> str:
        return f"strategy_symbol_direction:{signal.symbol}:{signal.direction}"

    @staticmethod
    def _family_cooldown_key(signal: Signal) -> str:
        family = catalog_setup_family(signal.setup_id)
        return f"family:{family}:{signal.symbol}:{signal.direction}"

    @staticmethod
    def _setup_interval_cooldown_key(setup_id: str) -> str:
        return f"setup_interval:{setup_id}"

    def _setup_interval_minutes(self, setup_id: str) -> int:
        intervals = getattr(self._bot.settings.delivery, "setup_interval_minutes", None) or {}
        base = int(intervals.get(setup_id, 0) or 0)
        if base <= 0:
            return 0
        repo = getattr(self._bot, "_modern_repo", None)
        if repo is None:
            return base
        getter = getattr(repo, "setup_win_rate", None)
        if not callable(getter):
            return base
        try:
            win_rate = getter(setup_id)
        except DEFENSIVE_EXC:
            return base
        if win_rate is None:
            return base
        if win_rate >= 0.60:
            return max(1, int(base * 0.7))
        if win_rate <= 0.35:
            return int(base * 1.5)
        return base

    def _contract_issue_rows(self, signal: Signal) -> list[dict[str, object]]:
        min_rr = float(self._bot.settings.filters.min_risk_reward)
        return [
            issue.to_dict() for issue in validate_signal_contract(signal, min_risk_reward=min_rr)
        ]

    def _limit_entry_gate(
        self,
        signal: Signal,
        prepared: PreparedSymbol | None,
    ) -> tuple[bool, str | None, dict[str, object]]:
        """Reject when the limit plan is invalidated or price already chased away."""
        mark_price = getattr(signal, "mark_price", None)
        if prepared is not None:
            mark_price = (
                mark_price if mark_price is not None else getattr(prepared, "mark_price", None)
            )
        chase_pct = resolve_late_entry_chase_pct(self._bot.settings)
        ready, reason, details = evaluate_publish_readiness(
            direction=str(signal.direction or ""),
            mark_price=float(mark_price) if mark_price is not None else None,
            entry_low=float(signal.entry_low),
            entry_high=float(signal.entry_high),
            stop=float(signal.stop),
            chase_pct=chase_pct,
            entry_order_type=str(getattr(signal, "entry_order_type", "limit") or "limit"),
        )
        return ready, reason, dict(details)

    def _new_portfolio_cap_state(self) -> dict[str, Any]:
        pinned_symbols = {
            str(item).strip().upper()
            for item in getattr(self._bot.settings.universe, "pinned_symbols", ())
        }
        return {
            "pinned": pinned_symbols,
            "counts": {},
            "symbol_counts": {},
            "direction_counts": {},
            "long_btc_downtrend": 0,
            "family_direction": {},
            "setup_counts": {},
            "family_counts": {},
        }

    def _passes_portfolio_cap(
        self,
        signal: Signal,
        state: dict[str, Any],
        *,
        max_signals: int = 80,
    ) -> tuple[bool, str | None]:
        if signal.symbol.upper() in state["pinned"]:
            return True, None
        direction = str(signal.direction or "").lower()
        regime = str(signal.btc_bias or "neutral").lower()
        key = (direction, regime)
        delivery = self._bot.settings.delivery
        max_correlated_direction = int(getattr(delivery, "portfolio_max_same_direction_regime", 4))
        max_family_direction = int(getattr(delivery, "portfolio_max_family_direction", 2))
        max_bear_longs = int(getattr(delivery, "portfolio_max_bear_longs", 2))
        if state["counts"].get(key, 0) >= max_correlated_direction:
            return False, "portfolio_direction_regime_cap"
        family = str(signal.strategy_family or "continuation")
        fam_key = (family, direction)
        if state["family_direction"].get(fam_key, 0) >= max_family_direction:
            return False, "portfolio_family_direction_cap"
        if direction == "long" and regime in {"downtrend", "bear"}:
            if state["long_btc_downtrend"] >= max_bear_longs:
                return False, "portfolio_long_btc_bear_cap"
            state["long_btc_downtrend"] += 1
        max_strategy_share = float(getattr(delivery, "max_strategy_share", 0.25) or 0.25)
        max_per_setup = max(1, int(max_signals * max_strategy_share))
        setup_count = int(state["setup_counts"].get(signal.setup_id, 0))
        if setup_count >= max_per_setup:
            return False, "portfolio_setup_share_cap"
        symbol_key = signal.symbol.upper()
        max_per_symbol = int(getattr(delivery, "portfolio_max_per_symbol", 2))
        symbol_count = int(state["symbol_counts"].get(symbol_key, 0))
        if symbol_count >= max_per_symbol:
            return False, "portfolio_symbol_cap"
        state["counts"][key] = int(state["counts"].get(key, 0)) + 1
        state["symbol_counts"][symbol_key] = symbol_count + 1
        state["direction_counts"][direction] = int(state["direction_counts"].get(direction, 0)) + 1
        state["family_direction"][fam_key] = int(state["family_direction"].get(fam_key, 0)) + 1
        state["setup_counts"][signal.setup_id] = setup_count + 1
        state["family_counts"][family] = int(state["family_counts"].get(family, 0)) + 1
        return True, None

    def _queue_ready_signal(
        self,
        signal: Signal,
        *,
        portfolio_state: dict[str, Any],
        ready_to_send: list[Signal],
        queued_setup_ids: set[str],
        queued_symbol_direction: set[str],
        rejected_rows: list[dict[str, Any]],
        symbol_direction_key: str | None = None,
        queued_family_keys: set[str] | None = None,
        family_key: str | None = None,
    ) -> bool:
        cap_ok, cap_reason = self._passes_portfolio_cap(
            signal, portfolio_state, max_signals=len(ready_to_send) + 80
        )
        if not cap_ok:
            rejected_rows.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": signal.symbol,
                    "setup_id": signal.setup_id,
                    "direction": signal.direction,
                    "stage": "portfolio",
                    "reason": cap_reason or "portfolio_cap_rejected",
                }
            )
            return False
        ready_to_send.append(signal)
        dedup_cache = getattr(self, "_dedup_timestamps", None)
        if dedup_cache is not None:
            dedup_key = f"{signal.setup_id}:{signal.symbol}:{signal.direction}"
            dedup_cache[dedup_key] = datetime.now(UTC)
        queued_setup_ids.add(signal.setup_id)
        queued_symbol_direction.add(
            symbol_direction_key or self._symbol_direction_cooldown_key(signal)
        )
        if queued_family_keys is not None:
            queued_family_keys.add(family_key or self._family_cooldown_key(signal))
        return True

    async def preload_ranking_cooldowns(self, all_candidates: dict[str, list[Signal]]) -> None:
        repo = getattr(self._bot, "_modern_repo", None)
        ages: dict[str, float] = {}
        if repo is None:
            self._bot._ranking_cooldown_ages = ages
            return
        now = datetime.now(UTC)
        seen: set[str] = set()
        for symbol_candidates in all_candidates.values():
            for signal in symbol_candidates:
                tracking_id = str(signal.tracking_id or "")
                if not tracking_id or tracking_id in seen:
                    continue
                seen.add(tracking_id)
                cooldown_key = f"{signal.setup_id}:{signal.symbol}"
                last_sent = await repo.get_cooldown(cooldown_key)
                if last_sent is None:
                    continue
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=UTC)
                ages[tracking_id] = max(
                    0.0, (now - last_sent.astimezone(UTC)).total_seconds() / 60.0
                )
        self._bot._ranking_cooldown_ages = ages

    def select_and_rank(
        self, all_candidates: dict[str, list[Signal]], max_signals: int
    ) -> list[Signal]:
        flat_candidates: list[Signal] = []
        for symbol_candidates in all_candidates.values():
            flat_candidates.extend(symbol_candidates)
        if not flat_candidates:
            return []

        by_setup: dict[str, list[Signal]] = {}
        portfolio_state = self._new_portfolio_cap_state()
        cooldown_ages = getattr(self._bot, "_ranking_cooldown_ages", None)
        if isinstance(cooldown_ages, dict):
            portfolio_state["cooldown_age_minutes"] = dict(cooldown_ages)
        for signal in sorted(
            flat_candidates,
            key=lambda item: self._rank_key(item, diversity_state=portfolio_state),
            reverse=True,
        ):
            by_setup.setdefault(signal.setup_id, []).append(signal)

        selected: list[Signal] = []
        selected_keys: set[str] = set()

        setup_lanes = sorted(
            by_setup.values(),
            key=lambda items: (
                self._rank_key(items[0], diversity_state=portfolio_state)
                if items
                else (0.0, 0.0, 0.0)
            ),
            reverse=True,
        )
        for setup_signals in setup_lanes:
            if len(selected) >= max_signals:
                break
            for signal in setup_signals:
                key = signal.signal_key
                if key in selected_keys:
                    continue
                cap_ok, _ = self._passes_portfolio_cap(
                    signal, portfolio_state, max_signals=max_signals
                )
                if not cap_ok:
                    continue
                selected.append(signal)
                selected_keys.add(key)
                break

        for signal in sorted(
            flat_candidates,
            key=lambda item: self._rank_key(item, diversity_state=portfolio_state),
            reverse=True,
        ):
            if len(selected) >= max_signals:
                break
            key = signal.signal_key
            if key in selected_keys:
                continue
            cap_ok, _ = self._passes_portfolio_cap(signal, portfolio_state, max_signals=max_signals)
            if not cap_ok:
                continue
            selected.append(signal)
            selected_keys.add(key)

        LOG.debug(
            "select_and_rank | candidates=%d symbols=%d setups=%d selected=%d setup_first=true",
            len(flat_candidates),
            len({signal.symbol for signal in flat_candidates}),
            len(by_setup),
            len(selected),
        )
        return selected
