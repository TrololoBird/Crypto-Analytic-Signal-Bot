"""Break of Structure / Change of Character (BOS/CHoCH) setup detector.

Uses _swing_points to classify swing structure on work_15m.
Focuses on CHoCH signals (structure reversal) as entry triggers.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""
from __future__ import annotations

import logging
import math

from ..setup_base import BaseSetup
from ..config import BotSettings
from ..models import PreparedSymbol, Signal
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..features import _swing_points
from ..setups.smc import latest_structure_break, swing_highs_lows
from ..setups.utils import get_dynamic_params

LOG = logging.getLogger("bot.strategies.bos_choch")

_MIN_SWINGS = 6   # Need 3+ of each type for trend context


def _select_external_stop_level(
    *,
    markers: list[object],
    levels: list[object],
    search_end: int,
    marker: float,
    price: float,
    above_price: bool,
) -> tuple[float | None, dict[str, object]]:
    """Select the latest external swing stop anchor and return reject diagnostics."""
    bounded_end = min(search_end, len(markers) - 1, len(levels) - 1)
    details: dict[str, object] = {
        "external_search_end": bounded_end,
        "external_marker": marker,
        "external_side_above_price": above_price,
    }
    if bounded_end < 0:
        details.update(
            external_marker_candidates=0,
            external_invalid_markers=0,
            external_invalid_levels=0,
            external_side_filtered=0,
        )
        return None, details

    marker_candidates = 0
    invalid_markers = 0
    invalid_levels = 0
    side_filtered = 0
    for idx in range(bounded_end, -1, -1):
        raw_marker = markers[idx]
        try:
            marker_value = float(raw_marker) if raw_marker is not None else 0.0
        except (TypeError, ValueError):
            invalid_markers += 1
            continue
        if raw_marker is None or marker_value != marker:
            continue
        marker_candidates += 1
        raw_level = levels[idx]
        if raw_level is None:
            invalid_levels += 1
            continue
        try:
            level = float(raw_level)
        except (TypeError, ValueError):
            invalid_levels += 1
            continue
        if not math.isfinite(level) or level <= 0.0:
            invalid_levels += 1
            continue
        if above_price and level > price:
            details.update(
                external_marker_candidates=marker_candidates,
                external_invalid_markers=invalid_markers,
                external_invalid_levels=invalid_levels,
                external_side_filtered=side_filtered,
            )
            details["external_selected_index"] = idx
            details["external_selected_level"] = level
            return level, details
        if not above_price and level < price:
            details.update(
                external_marker_candidates=marker_candidates,
                external_invalid_markers=invalid_markers,
                external_invalid_levels=invalid_levels,
                external_side_filtered=side_filtered,
            )
            details["external_selected_index"] = idx
            details["external_selected_level"] = level
            return level, details
        side_filtered += 1
    details.update(
        external_marker_candidates=marker_candidates,
        external_invalid_markers=invalid_markers,
        external_invalid_levels=invalid_levels,
        external_side_filtered=side_filtered,
    )
    return None, details


class BOSCHOCHSetup(BaseSetup):
    """BOS/CHoCH strategy detector for structural break signals."""

    setup_id = "bos_choch"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.55,
            "swing_lookback": 6,
            "external_swing_lookback": 20,
            "bos_lookback": 6,  # Backward-compatible alias.
            "choch_lookback": 6,  # Backward-compatible alias.
            "sl_buffer_atr": 0.2,
            "breakout_threshold_atr": 0.4,
            "bias_mismatch_penalty": 0.75,
            "min_rr": 1.5,
            "min_swings": 3,
        }
        if settings is not None:
            filters = getattr(settings, 'filters', None)
            if filters:
                setups_config = getattr(filters, 'setups', {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        """Detect BOS/CHoCH signal for given symbol."""
        try:
            return self._detect(prepared, settings)
        except (ValueError, KeyError, IndexError) as e:
            LOG.exception("%s bos_choch: detection error: %s", prepared.symbol, e)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(e).__name__,
            )
            return None

    def _detect(self, prepared: PreparedSymbol, _settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        dynamic_params = get_dynamic_params(prepared, setup_id)
        defaults = self.get_optimizable_params(_settings)

        configured_swing_lookback = int(dynamic_params.get("swing_lookback", defaults["swing_lookback"]))
        bos_lookback = int(dynamic_params.get("bos_lookback", configured_swing_lookback))
        choch_lookback = int(dynamic_params.get("choch_lookback", configured_swing_lookback))
        swing_lookback = max(2, max(bos_lookback, choch_lookback, configured_swing_lookback))
        external_swing_lookback = max(
            swing_lookback + 1,
            int(dynamic_params.get("external_swing_lookback", defaults["external_swing_lookback"])),
        )
        sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
        min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
        base_score = float(dynamic_params.get("base_score", defaults["base_score"]))

        w = prepared.work_15m
        if w.height < 30:
            _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
            return None

        atr = float(w.item(-1, "atr14") or 0.0)
        if atr <= 0 or math.isnan(atr):
            _reject(prepared, setup_id, "atr_invalid", atr=atr)
            return None

        price = prepared.mark_price or prepared.universe.last_price
        if not price or price <= 0:
            _reject(prepared, setup_id, "price_missing")
            return None

        sh_mask, sl_mask = _swing_points(w, n=swing_lookback)
        sh_prices = w.filter(sh_mask)["high"]
        sl_prices = w.filter(sl_mask)["low"]

        # Need at least 3 of each to determine prior trend + break
        min_swings = int(dynamic_params.get("min_swings", defaults["min_swings"]))
        if sh_prices.len() < min_swings or sl_prices.len() < min_swings:
            _reject(
                prepared,
                setup_id,
                "insufficient_swing_points",
                swing_highs=sh_prices.len(),
                swing_lows=sl_prices.len(),
                min_swings=min_swings,
            )
            return None

        sh_vals = sh_prices.to_numpy()
        sl_vals = sl_prices.to_numpy()

        structure_zone = latest_structure_break(
            w,
            swing_length=swing_lookback,
            prefer_kind="choch",
        )
        if structure_zone is None or structure_zone.kind != "choch":
            _reject(prepared, setup_id, "no_choch_detected")
            return None
        direction = structure_zone.direction
        stop_price = None
        pivot_level = None

        external_swings = swing_highs_lows(
            w,
            swing_length=external_swing_lookback,
            mode="live_safe",
        )
        external_markers = external_swings["HighLow"].to_list()
        external_levels = external_swings["Level"].to_list()
        external_search_end = min(
            int(structure_zone.broken_index or (w.height - 1)),
            len(external_markers) - 1,
            len(external_levels) - 1,
        )

        # --- Compute structural SL/TP ---
        if direction == "long":
            # SL uses external SMC swing structure only; internal CHoCH swings are not stop anchors.
            pivot_level, stop_details = _select_external_stop_level(
                markers=external_markers,
                levels=external_levels,
                search_end=external_search_end,
                marker=-1.0,
                price=price,
                above_price=False,
            )
            if pivot_level is None:
                _reject(
                    prepared,
                    setup_id,
                    "external_swing_stop_missing_long",
                    external_swing_lookback=external_swing_lookback,
                    **stop_details,
                )
                return None
            stop_price = pivot_level - sl_buffer_atr * atr
            risk = price - stop_price
            if risk <= 0:
                _reject(prepared, setup_id, "risk_non_positive_long", stop=stop_price, price=price)
                return None
            # TP1: last swing high before the structural break
            tp1 = float(sh_vals[-2]) if sh_vals[-2] > price else None
            # TP2: 4h swing target
            w4h = prepared.work_4h
            tp2 = None
            if w4h is not None and w4h.height > 5:
                sh4_mask, _ = _swing_points(w4h, n=2)
                sh4_prices = w4h.filter(sh4_mask)["high"]
                tp2_cands = sh4_prices.filter(sh4_prices > price)
                tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
        else:
            # SL uses external SMC swing structure only; internal CHoCH swings are not stop anchors.
            pivot_level, stop_details = _select_external_stop_level(
                markers=external_markers,
                levels=external_levels,
                search_end=external_search_end,
                marker=1.0,
                price=price,
                above_price=True,
            )
            if pivot_level is None:
                _reject(
                    prepared,
                    setup_id,
                    "external_swing_stop_missing_short",
                    external_swing_lookback=external_swing_lookback,
                    **stop_details,
                )
                return None
            stop_price = pivot_level + sl_buffer_atr * atr
            risk = stop_price - price
            if risk <= 0:
                _reject(prepared, setup_id, "risk_non_positive_short", stop=stop_price, price=price)
                return None
            # TP1: last swing low before the structural break
            tp1 = float(sl_vals[-2]) if sl_vals[-2] < price else None
            # TP2: 4h swing target
            w4h = prepared.work_4h
            tp2 = None
            if w4h is not None and w4h.height > 5:
                _, sl4_mask = _swing_points(w4h, n=2)
                sl4_prices = w4h.filter(sl4_mask)["low"]
                tp2_cands = sl4_prices.filter(sl4_prices < price)
                tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

        fallback_note = None
        if tp1 is None or abs(tp1 - price) < risk * min_rr:
            tp1 = price + risk * min_rr if direction == "long" else price - risk * min_rr
            fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
        if tp2 is None:
            tp2 = tp1  # Use TP1 as TP2 if no extended target found

        vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
        rsi = float(w.item(-1, "rsi14") or 50.0)
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )

        reasons = [
            f"CHoCH {direction}: structure reversal level={structure_zone.level:.4f}",
            f"external_swing_sl={pivot_level:.4f}",
            f"sh[-3]={sh_vals[-3]:.4f} sh[-2]={sh_vals[-2]:.4f} sh[-1]={sh_vals[-1]:.4f}",
            f"sl[-3]={sl_vals[-3]:.4f} sl[-2]={sl_vals[-2]:.4f} sl[-1]={sl_vals[-1]:.4f}",
        ]
        if fallback_note:
            reasons.append(fallback_note)

        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            score=score,
            timeframe="15m",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop_price,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price,
            atr=atr,
        )
