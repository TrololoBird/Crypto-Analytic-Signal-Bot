"""Expansion Engine pinned TG alerts — separate from Verdict deep change messages."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from hunt_core.analysis.expansion_engine.config import ExpansionConfig, load_expansion_config
from hunt_core.paths import EXPANSION_ALERT_STATE

LOG = structlog.get_logger("hunt.expansion_alerts")


def _expansion_dict(row: dict[str, Any]) -> dict[str, Any]:
    exp = row.get("expansion")
    return exp if isinstance(exp, dict) else {}


def expansion_change_fingerprint(exp: dict[str, Any]) -> str:
    """Hash material expansion state for change-only TG policy."""
    meta = exp.get("meta") if isinstance(exp.get("meta"), dict) else {}
    probs = exp.get("probabilities") if isinstance(exp.get("probabilities"), dict) else {}
    execution = exp.get("execution") if isinstance(exp.get("execution"), dict) else {}
    activation = 0.0
    if execution.get("activation") is not None:
        try:
            activation = round(float(execution["activation"]), 4)
        except (TypeError, ValueError):
            pass
    payload = {
        "state": str(exp.get("state") or "neutral"),
        "dominant": str(exp.get("dominant") or "neutral"),
        "stage": int(exp.get("lifecycle_stage") or 0),
        "trig": round(float(exp.get("trigger_probability") or 0), 2),
        "opp": round(float(meta.get("opportunity_score") or 0), 2),
        "qual": round(float(meta.get("expansion_quality") or 0), 2),
        "p_up": round(float(probs.get("p_up") or 0), 2),
        "p_down": round(float(probs.get("p_down") or 0), 2),
        "act": activation,
    }
    return json.dumps(payload, sort_keys=True)


def expansion_alert_eligible(exp: dict[str, Any], cfg: ExpansionConfig) -> bool:
    """Only alert on setups worth operator attention."""
    if not exp:
        return False
    meta = exp.get("meta") if isinstance(exp.get("meta"), dict) else {}
    quality = float(meta.get("expansion_quality") or 0.0)
    trigger = float(exp.get("trigger_probability") or 0.0)
    fake = float(meta.get("fake_breakout_risk") or 0.0)
    if fake >= cfg.fake_breakout_block:
        return False
    state = str(exp.get("state") or "neutral")
    if state in {"pre_pump", "pre_dump", "active_pump", "active_dump"}:
        return quality >= cfg.tg_min_quality
    if state in {"accumulation", "distribution"}:
        return quality >= cfg.tg_min_quality and trigger >= cfg.tg_min_trigger
    dominant = str(exp.get("dominant") or "neutral")
    return (
        dominant != "neutral"
        and quality >= cfg.tg_min_quality
        and trigger >= cfg.tg_min_trigger
    )


def material_expansion_change(
    symbol: str,
    row: dict[str, Any],
    *,
    prev: dict[str, Any] | None,
    cfg: ExpansionConfig | None = None,
    now: datetime | None = None,
) -> bool:
    """True when expansion state crossed alert thresholds or materially shifted."""
    _ = symbol
    cfg = cfg or load_expansion_config()
    if not cfg.enabled or not cfg.tg_pinned_alerts:
        return False
    exp = _expansion_dict(row)
    if not expansion_alert_eligible(exp, cfg):
        return False
    if not cfg.tg_on_change:
        return True
    if prev is None:
        return True
    prev_exp = _expansion_dict(prev)
    if expansion_change_fingerprint(exp) != expansion_change_fingerprint(prev_exp):
        return True
    state = str(exp.get("state") or "neutral")
    if state not in {"pre_pump", "pre_dump", "active_pump", "active_dump"}:
        return False
    now = now or datetime.now(UTC)
    ts = row.get("ts") or prev.get("ts")
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age_h = (now - dt).total_seconds() / 3600.0
        return age_h >= cfg.tg_stale_hours
    except (TypeError, ValueError):
        return False


def _load_alert_state() -> dict[str, Any]:
    if not EXPANSION_ALERT_STATE.is_file():
        return {"sent": {}}
    try:
        raw = json.loads(EXPANSION_ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent": {}}
    if not isinstance(raw, dict):
        return {"sent": {}}
    raw.setdefault("sent", {})
    raw.setdefault("fingerprints", {})
    return raw


def _save_alert_state(state: dict[str, Any]) -> None:
    try:
        EXPANSION_ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        EXPANSION_ALERT_STATE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def expansion_cooldown_ok(
    symbol: str,
    cfg: ExpansionConfig | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    cfg = cfg or load_expansion_config()
    sym = str(symbol or "").upper()
    if not sym or cfg.tg_cooldown_min <= 0:
        return True
    state = _load_alert_state()
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    raw_ts = sent.get(sym)
    if not raw_ts:
        return True
    now = now or datetime.now(UTC)
    try:
        last = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    return (now - last) >= timedelta(minutes=cfg.tg_cooldown_min)


def last_alert_fingerprint(symbol: str) -> str | None:
    sym = str(symbol or "").upper()
    if not sym:
        return None
    state = _load_alert_state()
    fps = state.get("fingerprints") if isinstance(state.get("fingerprints"), dict) else {}
    raw = fps.get(sym)
    return str(raw) if raw else None


def mark_expansion_alert_sent(
    symbol: str,
    *,
    fingerprint: str | None = None,
    now: datetime | None = None,
) -> None:
    sym = str(symbol or "").upper()
    if not sym:
        return
    state = _load_alert_state()
    sent = state.setdefault("sent", {})
    if not isinstance(sent, dict):
        sent = {}
        state["sent"] = sent
    sent[sym] = (now or datetime.now(UTC)).isoformat()
    if fingerprint:
        fps = state.setdefault("fingerprints", {})
        if not isinstance(fps, dict):
            fps = {}
            state["fingerprints"] = fps
        fps[sym] = fingerprint
    _save_alert_state(state)


async def send_expansion_change_telegram(
    broadcaster: Any,
    row: dict[str, Any],
) -> bool:
    """Send Expansion card to lab channel (E1 — not production TG)."""
    from hunt_core.analysis.expansion_engine.format import format_expansion_card
    from hunt_core.scanner.delivery.lab import send_lane_html

    sym = str(row.get("symbol") or "").upper()
    exp = _expansion_dict(row)
    if row.get("error") or not exp:
        return False
    exp = dict(exp)
    exp["lab_alert"] = True
    row["expansion"] = exp
    body = format_expansion_card(exp)
    text = f"🧨 <b>Expansion Alert</b> — lab\n\n{body}"
    setup = {"delivery_lane": "lab"}
    result = await send_lane_html(
        broadcaster,
        text,
        setup=setup,
        row=row,
        no_split=True,
    )
    if result.status == "sent":
        LOG.info("expansion_pinned_tg_sent", symbol=sym, message_id=result.message_id)
        return True
    LOG.warning(
        "expansion_pinned_tg_failed",
        symbol=sym,
        status=result.status,
        reason=result.reason,
    )
    return False


__all__ = [
    "expansion_alert_eligible",
    "expansion_change_fingerprint",
    "expansion_cooldown_ok",
    "last_alert_fingerprint",
    "mark_expansion_alert_sent",
    "material_expansion_change",
    "send_expansion_change_telegram",
]
