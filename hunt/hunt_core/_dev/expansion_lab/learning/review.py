"""Automatic outcome review at 24h / 48h / 72h / 7d horizons."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hunt_core._dev.expansion_lab.learning.outcome_tracker import (
    REVIEW_HORIZONS_H,
    grade_record,
    load_expansion_outcomes,
    persist_expansion_outcomes,
)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _graded_horizons(record: dict[str, Any]) -> set[int]:
    done: set[int] = set()
    for g in record.get("graded") or []:
        if not isinstance(g, dict):
            continue
        h = g.get("horizon_h")
        if h is not None:
            try:
                done.add(int(h))
            except (TypeError, ValueError):
                pass
    return done


def pending_review_horizons(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[tuple[int, float]]:
    """Return (horizon_h, elapsed_h) pairs that still need grading."""
    now = now or datetime.now(UTC)
    signal_ts = _parse_ts(str(record.get("ts") or ""))
    if signal_ts is None:
        return []
    elapsed_h = (now - signal_ts).total_seconds() / 3600.0
    if elapsed_h <= 0:
        return []
    done = _graded_horizons(record)
    pending: list[tuple[int, float]] = []
    for h in REVIEW_HORIZONS_H:
        if h in done:
            continue
        if elapsed_h >= float(h):
            pending.append((h, elapsed_h))
    return pending


def review_records_with_prices(
    records: list[dict[str, Any]],
    price_by_sym: dict[str, float],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grade all due horizons using a pre-fetched price map. Mutates *records* in place."""
    now = now or datetime.now(UTC)
    graded_n = 0
    missing_price = 0
    for rec in records:
        sym = str(rec.get("symbol") or "").upper()
        price = price_by_sym.get(sym)
        if price is None or price <= 0:
            if pending_review_horizons(rec, now=now):
                missing_price += 1
            continue
        for horizon_h, elapsed_h in pending_review_horizons(rec, now=now):
            grade = grade_record(rec, price_now=float(price), elapsed_h=elapsed_h)
            grade["horizon_h"] = horizon_h
            grade["reviewed_at"] = now.isoformat()
            graded = list(rec.get("graded") or [])
            graded.append(grade)
            rec["graded"] = graded
            graded_n += 1
    return {
        "records": len(records),
        "graded": graded_n,
        "missing_price": missing_price,
    }


async def review_expansion_outcomes(
    client: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch live prices and grade all due outcome reviews."""
    now = now or datetime.now(UTC)
    records = load_expansion_outcomes()
    if not records:
        return {"records": 0, "graded": 0, "missing_price": 0}

    need_syms: set[str] = set()
    for rec in records:
        if pending_review_horizons(rec, now=now):
            sym = str(rec.get("symbol") or "").upper()
            if sym:
                need_syms.add(sym)
    if not need_syms:
        return {"records": len(records), "graded": 0, "missing_price": 0}

    from hunt_core.data.collect import safe_fetch

    ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="expansion_review_ticker") or []
    price_by_sym: dict[str, float] = {}
    for t in ticker_raw:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or "").upper()
        if sym not in need_syms:
            continue
        try:
            price_by_sym[sym] = float(t.get("lastPrice") or t.get("last") or 0.0)
        except (TypeError, ValueError):
            continue

    summary = review_records_with_prices(records, price_by_sym, now=now)
    if summary.get("graded", 0) > 0:
        persist_expansion_outcomes(records)
        try:
            from hunt_core._dev.expansion_lab.learning.calibration import (
                maybe_refresh_calibration,
            )

            cal = maybe_refresh_calibration()
            if cal.get("status") == "ok":
                summary["calibration"] = "refreshed"
        except Exception:
            pass
    return summary


__all__ = [
    "pending_review_horizons",
    "review_expansion_outcomes",
    "review_records_with_prices",
]
