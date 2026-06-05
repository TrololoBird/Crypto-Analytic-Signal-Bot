"""Delivery ranking and portfolio cap helpers (extracted from delivery_orchestrator)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.delivery.contract import validate_signal_contract
from bot.delivery.tiers import rank_key as tier_rank_key
from bot.delivery.trade_plan import evaluate_publish_readiness
from bot.domain.limit_entry import resolve_late_entry_chase_pct

if TYPE_CHECKING:
    from bot.domain.schemas import PreparedSymbol, Signal
    from bot.runtime.bot import SignalBot


LOG = logging.getLogger("bot.runtime.bot")


class DeliveryRankingMixin:
    """Setup-first candidate ranking with portfolio caps."""

    _bot: SignalBot

    @staticmethod
    def _rank_key(signal: Signal) -> tuple[float, float, float]:
        return tier_rank_key(signal)

    @staticmethod
    def _symbol_direction_cooldown_key(signal: Signal) -> str:
        return f"strategy_symbol_direction:{signal.symbol}:{signal.direction}"

    @staticmethod
    def _contract_issue_rows(signal: Signal) -> list[dict[str, object]]:
        return [issue.to_dict() for issue in validate_signal_contract(signal)]

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
            "long_btc_downtrend": 0,
            "family_direction": {},
        }

    def _passes_portfolio_cap(
        self,
        signal: Signal,
        state: dict[str, Any],
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
        state["counts"][key] = int(state["counts"].get(key, 0)) + 1
        state["family_direction"][fam_key] = int(state["family_direction"].get(fam_key, 0)) + 1
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
    ) -> bool:
        cap_ok, cap_reason = self._passes_portfolio_cap(signal, portfolio_state)
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
        queued_setup_ids.add(signal.setup_id)
        queued_symbol_direction.add(
            symbol_direction_key or self._symbol_direction_cooldown_key(signal)
        )
        return True

    def select_and_rank(
        self, all_candidates: dict[str, list[Signal]], max_signals: int
    ) -> list[Signal]:
        flat_candidates: list[Signal] = []
        for symbol_candidates in all_candidates.values():
            flat_candidates.extend(symbol_candidates)
        if not flat_candidates:
            return []

        by_setup: dict[str, list[Signal]] = {}
        for signal in sorted(flat_candidates, key=self._rank_key, reverse=True):
            by_setup.setdefault(signal.setup_id, []).append(signal)

        selected: list[Signal] = []
        selected_keys: set[str] = set()
        portfolio_state = self._new_portfolio_cap_state()

        setup_lanes = sorted(
            by_setup.values(),
            key=lambda items: self._rank_key(items[0]) if items else (0.0, 0.0),
            reverse=True,
        )
        for setup_signals in setup_lanes:
            if len(selected) >= max_signals:
                break
            for signal in setup_signals:
                key = signal.signal_key
                if key in selected_keys:
                    continue
                cap_ok, _ = self._passes_portfolio_cap(signal, portfolio_state)
                if not cap_ok:
                    continue
                selected.append(signal)
                selected_keys.add(key)
                break

        for signal in sorted(flat_candidates, key=self._rank_key, reverse=True):
            if len(selected) >= max_signals:
                break
            key = signal.signal_key
            if key in selected_keys:
                continue
            cap_ok, _ = self._passes_portfolio_cap(signal, portfolio_state)
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
