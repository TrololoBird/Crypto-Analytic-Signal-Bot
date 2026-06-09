"""Stop-loss post-mortem messages for operator private DMs (not the signal channel)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot.persistence.sl_diagnostics import classify_stop_loss_root_cause

if TYPE_CHECKING:
    from bot.persistence.tracking import SignalTrackingEvent


def _minutes_between(start: Any, end: Any) -> int:
    if start is None or end is None:
        return 0
    try:
        return max(0, int((end - start).total_seconds() / 60))
    except TypeError, AttributeError, ValueError:
        return 0


def build_sl_postmortem_html(event: SignalTrackingEvent) -> str:
    """Detailed SL post-mortem for operator DM only."""
    tracked = event.tracked
    features: dict[str, Any] = {}
    feat_obj = getattr(tracked, "features", None)
    if feat_obj is not None and hasattr(feat_obj, "to_dict"):
        features = feat_obj.to_dict()
    created_at = getattr(tracked, "created_at", None)
    activated_at = getattr(tracked, "activated_at", None)
    closed_at = getattr(tracked, "closed_at", None) or getattr(event, "occurred_at", None)
    time_to_entry = _minutes_between(created_at, activated_at)
    time_to_exit = _minutes_between(created_at, closed_at)
    mfe = float(getattr(tracked, "max_favorable_pct", 0.0) or 0.0)
    mae = float(getattr(tracked, "max_adverse_pct", 0.0) or 0.0)
    sl_diag = classify_stop_loss_root_cause(
        direction=str(getattr(tracked, "direction", "") or ""),
        mfe=mfe,
        mae=mae,
        time_to_entry_min=time_to_entry,
        time_to_exit_min=time_to_exit,
        features=features,
    )
    symbol = str(getattr(tracked, "symbol", "") or "?")
    setup = str(getattr(tracked, "setup_id", "") or "?")
    direction = str(getattr(tracked, "direction", "") or "?")
    ref = str(getattr(tracked, "tracking_ref", "") or "")
    price = getattr(event, "event_price", None)
    price_txt = f"{float(price):.6g}" if price is not None else "-"
    lines = [
        "<b>POST-MORTEM · STOP</b>",
        f"{symbol} {direction.upper()} · <code>{setup}</code> · <code>#{ref}</code>",
        f"Exit: <code>{price_txt}</code> · MFE <code>{mfe:.2f}%</code> · "
        f"MAE <code>{mae:.2f}%</code>",
        f"Причина: <b>{sl_diag.get('label') or sl_diag.get('code') or '-'}</b>",
    ]
    reasons = sl_diag.get("reasons") or []
    if reasons:
        lines.append(f"Detail: <code>{reasons[0][:160]}</code>")
    post_sl = features.get("post_sl_favorable_pct")
    if post_sl is not None:
        lines.append(f"Post-SL bounce: <code>{float(post_sl):.2f}%</code>")
    lines.append("<i>Operator analytics · not for signal channel</i>")
    return "\n".join(lines)
