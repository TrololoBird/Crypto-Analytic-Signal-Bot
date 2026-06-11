"""Early dump/pump alerts — preparation and start before full confirm.

Short: exhaustion fade + dump initiation.
Long: initial impulse pump (BEAT/VELVET leg start), breakout arming, post-dump bounce.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from hunt_watch.param_store import effective_hunt_params

EarlyKind = Literal["none", "prep", "imminent", "start", "confirm"]

SHORT_PREP_LC = frozenset({"exhaustion_at_high", "distribution"})
LONG_PREP_LC = frozenset({
    "post_dump_bounce",
    "accumulation",
    "recovery",
    "breakout_arming",
    "impulse_initiating",
})

SHORT_PREP_SETUP = frozenset({"exhaustion_watch", "dump_setup_forming"})
SHORT_START_SETUP = frozenset({"dump_imminent", "dump_initiating"})
LONG_PREP_SETUP = frozenset({"accumulation_watch", "long_setup_forming"})
LONG_START_SETUP = frozenset({"long_imminent", "long_initiating"})

EARLY_COOLDOWN_MIN = {
    "prep": 30,
    "imminent": 20,
    "start": 25,
}

# Prep/start spam without tracker outcomes — confirmed entry only in Telegram.
EARLY_TELEGRAM_ENABLED = False
TP1_PARTIAL_FIX_PCT = 80


@dataclass(frozen=True, slots=True)
class EarlyAlert:
    kind: EarlyKind
    tier: str  # prep | imminent | start
    message: str


def _lc(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    phase = getattr(lifecycle, "phase", None)
    if hasattr(phase, "value"):
        phase = phase.value
    return {
        "phase": phase,
        "recommended_bias": getattr(lifecycle, "recommended_bias", None),
        "short_entry_ok": getattr(lifecycle, "short_entry_ok", None),
        "fall_from_high_pct": getattr(lifecycle, "fall_from_high_pct", None),
        "bounce_from_low_pct": getattr(lifecycle, "bounce_from_low_pct", None),
    }


def _fuel(setup: dict[str, Any], direction: str) -> float:
    if direction == "short":
        return float(setup.get("dump_fuel") or setup.get("dump_score") or 0)
    return float(setup.get("long_fuel") or setup.get("long_score") or 0)


def _ignition_pump(row: dict[str, Any] | None) -> dict[str, Any]:
    ign = (row or {}).get("ignition") or {}
    if str(ign.get("direction") or "") == "pump" and ign.get("active"):
        return ign
    return {}


def evaluate_early_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> EarlyAlert:
    """Whether to send preparation/start Telegram (separate from full confirm)."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lc(lifecycle)
    lc_phase = str(lc.get("phase") or "")
    setup_phase = str(setup.get("phase") or "")
    fuel = _fuel(setup, direction)
    confirmed = bool(setup.get("confirmed"))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    triggers = [str(t) for t in (setup.get("triggers") or [])]

    if direction == "short":
        if lc_phase not in SHORT_PREP_LC and not (
            lc_phase == "dump_active" and setup_phase in SHORT_START_SETUP
        ):
            return EarlyAlert("none", "", "")
        if lc_phase in SHORT_PREP_LC and not lc.get("short_entry_ok", True):
            return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        if setup_phase in SHORT_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in (
                    "close_below_support",
                    "live_below_support",
                    "rejection",
                    "cascade",
                    "lost_support",
                )
            )
            if has_struct or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Дамп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if setup_phase == "dump_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Дамп imminent · fuel {fuel:.0f} · жди closed-bar",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            fall = float(lc.get("fall_from_high_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка шорта · {lc_phase} · fuel {fuel:.0f} · fall {fall:.1f}%",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and fuel >= cal.confirm_min_score - 5
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming", "exhaustion_watch"}
        ):
            return EarlyAlert(
                "prep",
                "prep",
                f"Fade-zone watch · fuel {fuel:.0f} · {setup_phase}",
            )

    else:
        ign = _ignition_pump(row)
        ign_pct = float(ign.get("price_delta_pct") or 0)
        long_ok_phase = lc_phase in LONG_PREP_LC or bool(ign)

        if not long_ok_phase:
            return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        broke_res = any("broke_resistance" in t for t in triggers)
        if setup_phase in LONG_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in ("close_above_resistance", "bounce", "cascade", "broke_resistance")
            )
            if has_struct or broke_res or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Памп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if ign and ign_pct >= 2.0 and fuel >= cal.forming_min_score:
            if broke_res or setup_phase in LONG_START_SETUP:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Ignition +{ign_pct:.1f}% · {setup_phase} · fuel {fuel:.0f}",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Ignition заряд +{ign_pct:.1f}% · {lc_phase or 'pump'} · fuel {fuel:.0f}",
            )

        if setup_phase == "long_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Памп imminent · fuel {fuel:.0f}",
            )

        if lc_phase == "impulse_initiating" and fuel >= cal.forming_min_score:
            rally = float(lc.get("bounce_from_low_pct") or 0)
            if broke_res or fuel >= cal.confirm_min_score - 8:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Импульс вверх · fuel {fuel:.0f} · rally {rally:.1f}%",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Импульс формируется · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

        if lc_phase == "breakout_arming" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "prep",
                "prep",
                f"База заряжена (squeeze) · fuel {fuel:.0f} · жди пробой",
            )

        if (
            setup_phase in LONG_PREP_SETUP | {"long_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            rally = float(lc.get("bounce_from_low_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка лонга · {lc_phase} · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

    return EarlyAlert("none", "", "")


def early_cooldown_ok(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> bool:
    if tier not in EARLY_COOLDOWN_MIN:
        return True
    # Tier hierarchy: after 🚨 start was sent, re-sending 🟡 prep for the same
    # symbol+direction inside the window is noise (prep↔start oscillation gave
    # 76 would-sends on a 2-symbol replay) — an equal-or-higher tier on cooldown
    # silences this one too.
    order = ("prep", "imminent", "start")
    rank = order.index(tier) if tier in order else 0
    for other in order[rank:]:
        key = f"early:{symbol.upper()}:{direction.lower()}:{other}"
        raw = state.get(key)
        if not raw:
            continue
        try:
            last = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if now - last < timedelta(minutes=EARLY_COOLDOWN_MIN.get(other, 30)):
            return False
    return True


def mark_early_sent(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> None:
    state[f"early:{symbol.upper()}:{direction.lower()}:{tier}"] = now.isoformat()


def format_early_telegram(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: Any | None,
    alert: EarlyAlert,
) -> str:
    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    lc = _lc(lifecycle)
    fuel = _fuel(setup, direction)
    price = row.get("price")
    chg = row.get("chg_24h_pct")
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    setup_phase = html.escape(str(setup.get("phase") or "—"))
    ign = _ignition_pump(row)
    ign_txt = (
        f" · ignition <code>+{float(ign.get('price_delta_pct') or 0):.1f}%</code>"
        if ign
        else ""
    )

    if direction == "short":
        badge = {"prep": "🟠", "imminent": "🔴", "start": "🚨"}.get(alert.tier, "🔴")
        title = {"prep": "DUMP PREP", "imminent": "DUMP IMMINENT", "start": "DUMP START"}.get(
            alert.tier, "DUMP WATCH"
        )
    else:
        badge = {"prep": "🟡", "imminent": "🟢", "start": "🚨"}.get(alert.tier, "🟢")
        title = {"prep": "PUMP PREP", "imminent": "PUMP IMMINENT", "start": "PUMP START"}.get(
            alert.tier, "PUMP WATCH"
        )

    hard = setup.get("confirm_hard") or []
    triggers = setup.get("triggers") or []
    hard_txt = html.escape(", ".join(str(h) for h in hard[:5]))
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))

    lines = [
        f"{badge} <b>{title}</b> {sym}",
        f"<i>{html.escape(alert.message)}</i>",
        f"Цена <code>{price}</code> · 24h <code>{chg}%</code>{ign_txt}",
        f"Lifecycle <code>{lc_phase}</code> · setup <code>{setup_phase}</code> · fuel <code>{fuel:.0f}</code>",
    ]
    if hard_txt:
        lines.append(f"Hard partial: <code>{hard_txt}</code>")
    if trig_txt:
        lines.append(f"Triggers: <code>{trig_txt}</code>")
    ez = setup.get("entry_zone") or []
    if len(ez) >= 2:
        lines.append(
            f"Entry zone <code>{ez[0]}</code>–<code>{ez[1]}</code> · "
            f"SL <code>{setup.get('stop_loss')}</code> · TP1 <code>{setup.get('tp1')}</code>"
        )
    lines.append("<i>Early hunt alert · prep/start — не auto-trade</i>")
    return "\n".join(lines)
