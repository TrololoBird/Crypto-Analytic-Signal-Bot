"""Dashboard tracking serialization - mark price, progress, PnL hints."""

from __future__ import annotations

from typing import Any


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _pick_price(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None and parsed > 0.0:
            return parsed
    return None


def resolve_mark_price(bot: Any, symbol: str, *, fallback: float | None = None) -> float | None:
    ws = getattr(bot, "_ws_manager", None)
    if ws is not None and symbol:
        snapshot_fn = getattr(ws, "get_mark_price_snapshot", None)
        if callable(snapshot_fn):
            snap = snapshot_fn(symbol)
            if isinstance(snap, dict):
                mark = _safe_float(snap.get("mark_price"))
                if mark is not None and mark > 0.0:
                    return mark
    return fallback


def compute_progress(
    *,
    direction: str,
    status: str,
    entry: float | None,
    stop: float | None,
    tp1: float | None,
    tp2: float | None,
    tp3: float | None,
    current: float | None,
    tp1_hit_at: Any,
    tp2_hit_at: Any,
) -> dict[str, Any]:
    """Human-readable progress for dashboard cards."""
    dir_norm = str(direction or "long").lower()
    is_long = dir_norm != "short"
    status_norm = str(status or "pending").lower()

    if current is None or current <= 0.0:
        return {
            "progress_pct": 0.0,
            "progress_label": "Нет цены",
            "progress_tone": "muted",
            "unrealized_pnl_pct": None,
            "next_target_label": None,
        }

    if status_norm == "pending":
        if entry is None:
            return {
                "progress_pct": 0.0,
                "progress_label": "Ждём входа",
                "progress_tone": "yellow",
                "unrealized_pnl_pct": None,
                "next_target_label": "Вход",
            }
        dist_pct = abs(current - entry) / entry * 100.0
        return {
            "progress_pct": min(100.0, dist_pct),
            "progress_label": f"Лимит · до зоны {dist_pct:.2f}%",
            "progress_tone": "yellow",
            "unrealized_pnl_pct": None,
            "next_target_label": "Лимит-вход",
        }

    if entry is None:
        return {
            "progress_pct": 0.0,
            "progress_label": "-",
            "progress_tone": "muted",
            "unrealized_pnl_pct": None,
            "next_target_label": None,
        }

    pnl_pct = (current - entry) / entry * 100.0 if is_long else (entry - current) / entry * 100.0

    if stop is not None:
        if is_long and current <= stop:
            return {
                "progress_pct": 100.0,
                "progress_label": "У стопа",
                "progress_tone": "red",
                "unrealized_pnl_pct": round(pnl_pct, 3),
                "next_target_label": "Стоп",
            }
        if not is_long and current >= stop:
            return {
                "progress_pct": 100.0,
                "progress_label": "У стопа",
                "progress_tone": "red",
                "unrealized_pnl_pct": round(pnl_pct, 3),
                "next_target_label": "Стоп",
            }

    targets: list[tuple[str, float | None, Any]] = [
        ("Цель 1", tp1, tp1_hit_at),
        ("Цель 2", tp2, tp2_hit_at),
        ("Цель 3", tp3, None),
    ]
    next_label = "Цель 1"
    next_price = tp1
    for label, price, hit_at in targets:
        if price is None:
            continue
        if hit_at:
            continue
        next_label = label
        next_price = price
        break
    else:
        return {
            "progress_pct": 100.0,
            "progress_label": "Все цели взяты",
            "progress_tone": "green",
            "unrealized_pnl_pct": round(pnl_pct, 3),
            "next_target_label": "Готово",
        }

    if next_price is None:
        return {
            "progress_pct": max(0.0, min(100.0, 50.0 if pnl_pct > 0 else 0.0)),
            "progress_label": f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%",
            "progress_tone": "green" if pnl_pct >= 0 else "red",
            "unrealized_pnl_pct": round(pnl_pct, 3),
            "next_target_label": None,
        }

    if is_long:
        span = next_price - entry
        travelled = current - entry
    else:
        span = entry - next_price
        travelled = entry - current

    if span <= 0:
        progress_pct = 100.0 if travelled > 0 else 0.0
    else:
        progress_pct = max(0.0, min(100.0, travelled / span * 100.0))

    tone = "green" if pnl_pct >= 0 else "red"
    return {
        "progress_pct": round(progress_pct, 1),
        "progress_label": f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}% · {next_label}",
        "progress_tone": tone,
        "unrealized_pnl_pct": round(pnl_pct, 3),
        "next_target_label": next_label,
    }


def serialize_tracking_signal(sig: dict[str, Any], bot: Any | None = None) -> dict[str, Any]:
    """Map DB row to dashboard-friendly tracking payload."""
    symbol = str(sig.get("symbol") or "")
    entry = _pick_price(
        sig.get("activation_price"),
        sig.get("entry_mid"),
        sig.get("entry_price"),
    )
    stop = _pick_price(sig.get("stop_price"), sig.get("stop"))
    tp1 = _pick_price(sig.get("tp1_price"), sig.get("take_profit_1"))
    tp2 = _pick_price(sig.get("tp2_price"), sig.get("take_profit_2"))
    tp3 = _pick_price(sig.get("tp3_price"), sig.get("take_profit_3"))

    last_price = _pick_price(sig.get("last_price"))
    mark_price = (
        resolve_mark_price(bot, symbol, fallback=last_price) if bot is not None else last_price
    )
    current = mark_price or last_price

    progress = compute_progress(
        direction=str(sig.get("direction") or "long"),
        status=str(sig.get("status") or "pending"),
        entry=entry,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        current=current,
        tp1_hit_at=sig.get("tp1_hit_at"),
        tp2_hit_at=sig.get("tp2_hit_at"),
    )

    return {
        "symbol": symbol,
        "setup_id": sig.get("setup_id"),
        "direction": sig.get("direction"),
        "timeframe": sig.get("timeframe") or "15m",
        "entry_price": entry,
        "entry_mid": _pick_price(sig.get("entry_mid")),
        "stop_price": stop,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "tp3_price": tp3,
        "score": sig.get("score"),
        "risk_reward": sig.get("risk_reward"),
        "status": sig.get("status"),
        "tracking_id": sig.get("tracking_id"),
        "tracking_ref": sig.get("tracking_ref"),
        "timestamp": sig.get("activated_at") or sig.get("created_at"),
        "created_at": sig.get("created_at"),
        "activated_at": sig.get("activated_at"),
        "pending_expires_at": sig.get("pending_expires_at") or sig.get("valid_until"),
        "tp1_hit_at": sig.get("tp1_hit_at"),
        "tp2_hit_at": sig.get("tp2_hit_at"),
        "close_reason": sig.get("close_reason"),
        "mark_price": mark_price,
        "last_price": last_price,
        "current_price": current,
        **progress,
    }
