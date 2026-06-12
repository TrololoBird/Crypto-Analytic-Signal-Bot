"""Stable contracts between production and research loops."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Direction = Literal["short", "long"]
BtOutcome = Literal["tp1_hit", "tp2_hit", "sl_hit", "timeout"]
CloseReason = Literal[
    "stop_hit",
    "tp1",
    "tp2",
    "invalidate",
    "lifecycle_stale",
    "bias_flip",
    "timeout",
    "manual",
    "reclaim",
]


class LifecycleBlock(TypedDict, total=False):
    phase: str
    recommended_bias: str
    short_entry_ok: bool
    long_entry_ok: bool
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None


class DumpBlock(TypedDict, total=False):
    phase: str
    score: float | None
    fuel: float | None
    triggers: list[str]
    confirm_hard: list[str]
    confirmed: bool
    entry_zone: list[float] | None
    support_break_level: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    invalidation_above: float | None
    levels_viable: bool
    levels_veto: str | None


class LongBlock(TypedDict, total=False):
    confirmed: bool
    score: float | None
    fuel: float | None
    entry_zone: list[float] | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None


class MarketBlock(TypedDict, total=False):
    taker_5m: float | None
    oi_chg_1h: float | None
    oi_z_score: float | None
    funding_pct: float | None
    top_ls_1h: float | None
    depth_imbalance: float | None
    liquidation_score_5m: float | None
    microprice_bias: float | None


class TickRow(TypedDict, total=False):
    ts: str
    symbol: str
    price: float
    chg_24h_pct: float | None
    range_24h_pct: float | None
    lifecycle: LifecycleBlock
    dump: DumpBlock
    long: LongBlock
    market: MarketBlock
    regime: dict[str, Any]
    session: dict[str, Any]
    book_walls: dict[str, Any]


class FeatureVector(TypedDict, total=False):
    ts: str | None
    price: float | None
    market: dict[str, Any]
    regime: dict[str, Any]
    lifecycle_phase: str | None
    lifecycle_bias: str | None
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None
    pos_in_range: float | None


class SignalRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    invalidation_above: float | None
    invalidation_below: float | None
    fuel: float | None
    entry_lifecycle_phase: str | None
    entry_lifecycle_bias: str | None
    close_reason: CloseReason | str | None
    exit_price: float | None
    pnl_pct: float | None
    mfe_pct: float | None
    duration_min: float | None
    extreme_hi: float | None
    extreme_lo: float | None
    entry_message_id: int | None
    opened_at: str | None
    closed_at: str | None
    features_open: FeatureVector
    features_peak: FeatureVector
    features_close: FeatureVector


class OutcomeRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    lifecycle_phase: str
    fuel: float | None
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    bt_outcome: BtOutcome
    bt_mfe_pct: float | None
    bt_mae_pct: float | None
    bt_candles_to_tp1: int | None
    opened_at: str | None
    source: str
    grade_id: str | None


def normalize_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    """Dedupe positioning==market; ensure nested dicts."""
    out = dict(row)
    market = out.get("market") or out.get("positioning") or {}
    if isinstance(market, dict):
        out["market"] = dict(market)
    out.pop("positioning", None)
    for key in ("lifecycle", "dump", "long", "regime", "session", "book_walls"):
        val = out.get(key)
        if val is not None and not isinstance(val, dict):
            out[key] = {}
    return out


def outcome_from_row(row: dict[str, Any], *, source: str) -> OutcomeRecord:
    """Build OutcomeRecord from graded JSONL row."""
    phase = row.get("lifecycle_phase") or row.get("entry_lifecycle_phase") or "unknown"
    return OutcomeRecord(
        symbol=str(row.get("symbol", "")),
        direction=row.get("direction", "short"),  # type: ignore[typeddict-item]
        lifecycle_phase=str(phase),
        fuel=row.get("fuel"),
        entry_lo=float(row.get("entry_lo") or row.get("entry_lo", 0)),
        entry_hi=float(row.get("entry_hi") or row.get("entry_hi", 0)),
        stop_loss=float(row.get("stop_loss") or 0),
        tp1=float(row.get("tp1") or 0),
        tp2=float(row.get("tp2") or 0),
        bt_outcome=row.get("bt_outcome", "timeout"),  # type: ignore[typeddict-item]
        bt_mfe_pct=row.get("bt_mfe_pct"),
        bt_mae_pct=row.get("bt_mae_pct"),
        bt_candles_to_tp1=row.get("bt_candles_to_tp1"),
        opened_at=row.get("opened_at"),
        source=source,
        grade_id=row.get("grade_id"),
    )
