"""Sticky lifecycle — debounce phase/bias flicker on meme perps (WLD post-mortem).

Raw ``assess_hunt_lifecycle`` can flip accumulation↔impulse↔dump_active every
60s poll. Cross-bucket transitions require consecutive agreeing ticks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_watch.lifecycle import HuntLifecycle, HuntPhase

# Cross-bucket flips (long_leg → dump) need more evidence than cosmetic renames.
_TICKS_CROSS_BUCKET = 3
_TICKS_SAME_BUCKET = 2
_TICKS_LONG_TO_DUMP = 4


def _phase_bucket(phase: str) -> str:
    if phase in {
        HuntPhase.ACCUMULATION.value,
        HuntPhase.IMPULSE_INITIATING.value,
        HuntPhase.BREAKOUT_ARMING.value,
        HuntPhase.POST_DUMP_BOUNCE.value,
        HuntPhase.RECOVERY.value,
    }:
        return "long_leg"
    if phase in {HuntPhase.EXHAUSTION_AT_HIGH.value, HuntPhase.DISTRIBUTION.value}:
        return "short_fade"
    if phase == HuntPhase.DUMP_ACTIVE.value:
        return "dump"
    return "other"


@dataclass(slots=True)
class _StickyEntry:
    phase: str
    bias: str
    pending_phase: str | None = None
    pending_count: int = 0


_store: dict[str, _StickyEntry] = {}


def _rebuild(raw: HuntLifecycle, *, phase: str, bias: str) -> HuntLifecycle:
    """Keep fresh metrics from raw tick; latch phase/bias flags to sticky values."""
    try:
        ph = HuntPhase(phase)
    except ValueError:
        ph = raw.phase

    short_entry_ok = ph in {HuntPhase.EXHAUSTION_AT_HIGH, HuntPhase.DISTRIBUTION}
    short_confirm_ok = ph in {
        HuntPhase.EXHAUSTION_AT_HIGH,
        HuntPhase.DISTRIBUTION,
        HuntPhase.DUMP_ACTIVE,
    } and not raw.invalidate_short
    if ph == HuntPhase.DUMP_ACTIVE:
        short_entry_ok = False

    return HuntLifecycle(
        phase=ph,
        recommended_bias=bias,  # type: ignore[arg-type]
        short_entry_ok=short_entry_ok,
        short_confirm_ok=short_confirm_ok,
        invalidate_short=raw.invalidate_short,
        fall_from_high_pct=raw.fall_from_high_pct,
        bounce_from_low_pct=raw.bounce_from_low_pct,
        local_support=raw.local_support,
        local_resistance=raw.local_resistance,
        reasons=(*raw.reasons, f"sticky_hold={phase}"),
    )


def stabilize(symbol: str, raw: HuntLifecycle) -> HuntLifecycle:
    """Return debounced lifecycle for watch tick."""
    sym = symbol.upper()
    raw_phase = raw.phase.value
    raw_bias = str(raw.recommended_bias or "wait")

    entry = _store.get(sym)
    if entry is None:
        _store[sym] = _StickyEntry(raw_phase, raw_bias)
        return raw

    if raw_phase == entry.phase:
        entry.pending_phase = None
        entry.pending_count = 0
        entry.bias = raw_bias
        return raw

    cur_bucket = _phase_bucket(entry.phase)
    new_bucket = _phase_bucket(raw_phase)
    need = _TICKS_SAME_BUCKET if cur_bucket == new_bucket else _TICKS_CROSS_BUCKET
    if cur_bucket == "long_leg" and new_bucket == "dump":
        need = _TICKS_LONG_TO_DUMP

    if entry.pending_phase != raw_phase:
        entry.pending_phase = raw_phase
        entry.pending_count = 1
    else:
        entry.pending_count += 1

    if entry.pending_count >= need:
        entry.phase = raw_phase
        entry.bias = raw_bias
        entry.pending_phase = None
        entry.pending_count = 0
        return raw

    return _rebuild(raw, phase=entry.phase, bias=entry.bias)


def reset_symbol(symbol: str) -> None:
    _store.pop(symbol.upper(), None)


def sticky_snapshot() -> dict[str, Any]:
    return {
        sym: {
            "phase": e.phase,
            "bias": e.bias,
            "pending": e.pending_phase,
            "pending_n": e.pending_count,
        }
        for sym, e in _store.items()
    }
