"""EV-primary delivery resolution — replaces fuel/min_fuel gate (X2)."""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

LOG = logging.getLogger(__name__)

from hunt_core.contract import compute_rule_based_ev
from hunt_core.params.store import delivery_thresholds

Direction = Literal["short", "long"]


def pwin_gate_enabled() -> bool:
    """When false (default), min_p_win is shadow-only — delivery uses playbook + RR."""
    return os.getenv("HUNT_PWIN_GATE", "0").strip().lower() in {"1", "true", "yes"}


def resolve_delivery_ev(
    setup: dict[str, Any],
    *,
    direction: Direction,
    row: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve EV + P for delivery from catalog-primary, shadow, or geometry recompute."""
    ev: float | None = None
    confidence_score: float | None = None
    source = "missing"

    for key in ("ev_primary_ev", "catalog_ev", "delivery_ev"):
        raw = setup.get(key)
        if raw is not None:
            try:
                ev = float(raw)
                source = key
                break
            except (TypeError, ValueError):
                LOG.debug("resolve_delivery_ev_parse_failed key=%s raw=%r", key, raw)
                continue

    shadow = setup.get("ev_shadow") if isinstance(setup.get("ev_shadow"), dict) else {}
    if ev is None and shadow.get("ev") is not None:
        try:
            ev = float(shadow["ev"])
            source = "ev_shadow"
        except (TypeError, ValueError):
            LOG.debug("resolve_delivery_ev_shadow_ev_parse_failed raw=%r", shadow.get("ev"))

    for key in ("delivery_confidence_score", "fusion_strength", "confidence_score", "catalog_confidence_score"):
        raw = setup.get(key)
        if raw is not None:
            try:
                confidence_score = float(raw)
                break
            except (TypeError, ValueError):
                LOG.debug("resolve_delivery_ev_pwin_parse_failed key=%s raw=%r", key, raw)
                continue
    if confidence_score is None and shadow.get("confidence_score") is not None:
        try:
            confidence_score = float(shadow["confidence_score"])
        except (TypeError, ValueError):
            LOG.debug("resolve_delivery_ev_shadow_pwin_parse_failed raw=%r", shadow.get("confidence_score"))

    struct = structure
    if struct is None and row is not None:
        struct = row.get("structure") if isinstance(row.get("structure"), dict) else None
    if (ev is None or confidence_score is None) and setup.get("stop_loss") and setup.get("tp1"):
        recomputed = compute_rule_based_ev(setup, direction=direction, structure=struct)
        if ev is None and recomputed.get("ev") is not None:
            ev = float(recomputed["ev"])
            source = "recomputed"
        if confidence_score is None and recomputed.get("confidence_score") is not None:
            confidence_score = float(recomputed["confidence_score"])

    return {
        "ev": ev,
        "confidence_score": confidence_score,
        "source": source,
        "reason": shadow.get("reason") if isinstance(shadow, dict) else None,
    }


def stamp_delivery_ev_fields(
    setup: dict[str, Any],
    *,
    direction: Direction,
    row: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
    catalog_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unify delivery_ev / delivery_confidence_score on setup after tick assembly."""
    if catalog_candidate:
        if catalog_candidate.get("ev") is not None:
            setup["catalog_ev"] = catalog_candidate["ev"]
        if catalog_candidate.get("confidence_score") is not None:
            setup["catalog_confidence_score"] = catalog_candidate["confidence_score"]
        if catalog_candidate.get("setup_id"):
            setup.setdefault("catalog_setup", catalog_candidate["setup_id"])
    resolved = resolve_delivery_ev(
        setup, direction=direction, row=row, structure=structure
    )
    if resolved["ev"] is not None:
        setup["delivery_ev"] = resolved["ev"]
    if resolved["confidence_score"] is not None:
        setup["delivery_confidence_score"] = resolved["confidence_score"]
    setup["delivery_ev_source"] = resolved["source"]
    return setup


def delivery_ev_floors(
    symbol: str,
    *,
    confirmed: bool,
) -> tuple[float, float]:
    """Return (min_ev, min_p_win) from calibrated delivery params."""
    dl = delivery_thresholds(symbol)
    min_ev = float(dl.get("min_ev", 0.0))
    if confirmed:
        min_p = float(dl.get("min_p_win", 0.42))
    else:
        min_p = float(dl.get("min_p_win_forming", dl.get("min_p_win", 0.42) * 0.85))
    return min_ev, min_p


def legacy_fuel_delivery_enabled() -> bool:
    return False


def setup_fusion_score(setup: dict[str, Any]) -> float | None:
    """Fusion-engine strength index 0–100 (not calibrated P(win))."""
    raw = setup.get("fusion_score")
    if raw is None:
        return None
    try:
        score = float(raw)
        if 0.0 <= score <= 100.0:
            return score
    except (TypeError, ValueError):
        LOG.debug("setup_fusion_score_parse_failed raw=%r", raw)
    return None


def setup_confidence_score(setup: dict[str, Any]) -> float | None:
    """Calibrated P(win) in [0, 1] — primary delivery strength."""
    for key in ("delivery_confidence_score", "fusion_strength", "confidence_score", "catalog_confidence_score"):
        raw = setup.get(key)
        if raw is None:
            continue
        try:
            p = float(raw)
            if 0.0 <= p <= 1.0:
                return p
        except (TypeError, ValueError):
            continue
    shadow = setup.get("ev_shadow") if isinstance(setup.get("ev_shadow"), dict) else {}
    if shadow.get("confidence_score") is not None:
        try:
            p = float(shadow["confidence_score"])
            if 0.0 <= p <= 1.0:
                return p
        except (TypeError, ValueError):
            LOG.debug("setup_confidence_score_shadow_parse_failed raw=%r", shadow.get("confidence_score"))
    return None


def setup_conviction_pct(setup: dict[str, Any], *, direction: str = "short") -> float:
    """0–100 conviction for display — fusion_score first, then calibrated P(win), then fuel."""
    fs = setup_fusion_score(setup)
    if fs is not None:
        return fs
    p = setup_confidence_score(setup)
    if p is not None:
        return min(100.0, max(0.0, p * 100.0))
    from hunt_core.scanner.gate._rr import setup_fuel_legacy

    return setup_fuel_legacy(setup, direction)


def strength_display_label(setup: dict[str, Any], *, direction: str = "short") -> str:
    """Operator-facing strength for early TG / logs."""
    fs = setup_fusion_score(setup)
    if fs is not None:
        return f"fusion {fs:.0f}"
    p = setup_confidence_score(setup)
    if p is not None:
        return f"P {p:.0%}"
    return f"conv {setup_conviction_pct(setup, direction=direction):.0f}"


# Phase-B3: legacy phase/fuel vetoes retired when EV-primary economics pass. The
# structure detectors + P(win)/EV floors subsume these; the genuine "do not trade"
# vetoes (bias/HTF/MTF must_pass, wash, kinematic, data_incomplete, rr_below_min,
# levels_veto, bad geometry, past_tp1, invalidate_short, watch_only) are NOT here and
# still apply.
EV_PRIMARY_LEGACY_BLOCKERS: frozenset[str] = frozenset(
    {
        # dump/phase-active short guards (EV + structure handle leg timing)
        "short_entry_not_ok",
        "dump_mid_leg",
        "dump_deep_chase",
        "dump_late_entry",
        "bias_wait_mid_dump",
        "premature_exhaustion",
        # long phase/chase guards
        "long_blocked_mid_dump",
        "long_below_hunt_high",
        "long_below_resistance",
        # fuel/confluence-era floors (replaced by P(win)/EV)
        "below_forming_min",
        "below_min_fuel",
        "delivery_fuel_low",
        "delivery_confluence_low",
        "prep_shadow_tighten",
        # phase-fade / impulse / accumulation fuel sub-gates
        "exhaustion_fade_weak",
        "exhaustion_strong_trend",
        "impulse_session_weak",
        "impulse_oi_weak",
        "accumulation_long_weak",
        # trend/anomaly/structural-disagreement (detector already found the structure)
        "filter_block",
        "not_anomaly",
        "not_at_level",
        "phase_matrix_disable",
        "scanner_continuation_wait",
    }
)


def ev_primary_delivery_qualified(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    row: dict[str, Any] | None = None,
) -> bool:
    """EV-primary path: structure detectors + P(win)/EV floors replace fuel/phase stack."""
    if not pwin_gate_enabled():
        from hunt_core.toolkit.playbook_eval import setup_meets_playbook

        if setup_meets_playbook(setup, row=row, direction=direction):  # type: ignore[arg-type]
            return True
        if not bool(setup.get("ev_primary")):
            return False
        resolved = resolve_delivery_ev(setup, direction=direction, row=row)  # type: ignore[arg-type]
        ev = resolved.get("ev")
        if ev is None:
            return False
        min_ev, _ = delivery_ev_floors(symbol, confirmed=bool(setup.get("impulse_confirmed")))
        try:
            return float(ev) > max(0.0, min_ev)
        except (TypeError, ValueError):
            return False
    if not bool(setup.get("ev_primary")):
        return False
    confidence_score = setup_confidence_score(setup)
    if confidence_score is None:
        return False
    confirmed = bool(setup.get("impulse_confirmed") or setup.get("intrabar_confirmed"))
    min_ev, min_p = delivery_ev_floors(symbol, confirmed=confirmed)
    if confidence_score < min_p:
        return False
    resolved = resolve_delivery_ev(setup, direction=direction)  # type: ignore[arg-type]
    ev = resolved.get("ev")
    if ev is None:
        return False
    try:
        return float(ev) > max(0.0, min_ev)
    except (TypeError, ValueError):
        return False


def filter_ev_primary_legacy_blockers(
    blockers: list[Any],
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    row: dict[str, Any] | None = None,
) -> list[Any]:
    """Drop phase/fuel-era codes when EV-primary delivery is qualified."""
    if not ev_primary_delivery_qualified(
        setup, direction=direction, symbol=symbol, row=row
    ):
        return blockers
    return [b for b in blockers if getattr(b, "code", None) not in EV_PRIMARY_LEGACY_BLOCKERS]


def setup_meets_strength(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    tier: Literal["forming", "confirm"] = "forming",
    boost_p: float = 0.0,
    boost_conv: float = 0.0,
    slack_p: float = 0.0,
    slack_conv: float = 0.0,
    row: dict[str, Any] | None = None,
) -> bool:
    """DEPRECATED for production confirm — use ``detect/setup_fields.setup_meets_strength``.

    This copy still references ``confirm_min_score`` for legacy report paths. Hot delivery
    imports ``setup_fields`` (fusion ``confirmed`` only). See docs/AUTHORITY_MODEL.md §1.
    """
    if not pwin_gate_enabled() and row is not None:
        from hunt_core.toolkit.playbook_eval import setup_meets_playbook

        if tier == "confirm" and setup_meets_playbook(
            setup, row=row, direction=direction  # type: ignore[arg-type]
        ):
            return True
    from hunt_core.params.store import effective_hunt_params

    sym = symbol.upper()
    dl = delivery_thresholds(sym)
    cal = effective_hunt_params(sym)
    if tier == "forming":
        min_p = float(dl.get("min_p_win_forming", 0.35)) + boost_p
        min_conv = float(cal.forming_min_score) + boost_conv
    else:
        min_p = float(dl.get("min_p_win", 0.42)) - slack_p
        min_conv = float(cal.confirm_min_score) - slack_conv
    if not pwin_gate_enabled():
        p = setup_confidence_score(setup)
        if tier == "confirm":
            if p is not None and p >= min_p - slack_p:
                return True
            if row is not None:
                from hunt_core.toolkit.playbook_eval import setup_meets_playbook

                if setup_meets_playbook(
                    setup, row=row, direction=direction  # type: ignore[arg-type]
                ):
                    return True
        if p is not None:
            shadow = setup.get("ev_shadow")
            if not isinstance(shadow, dict):
                setup["ev_shadow"] = {"confidence_score": p}
            else:
                shadow.setdefault("confidence_score", p)
        return setup_conviction_pct(setup, direction=direction) >= min_conv - slack_conv
    p = setup_confidence_score(setup)
    if p is not None:
        return p >= min_p
    return setup_conviction_pct(setup, direction=direction) >= min_conv


__all__ = [
    "EV_PRIMARY_LEGACY_BLOCKERS",
    "delivery_ev_floors",
    "ev_primary_delivery_qualified",
    "filter_ev_primary_legacy_blockers",
    "legacy_fuel_delivery_enabled",
    "pwin_gate_enabled",
    "resolve_delivery_ev",
    "setup_conviction_pct",
    "setup_fusion_score",
    "setup_meets_strength",
    "setup_confidence_score",
    "stamp_delivery_ev_fields",
    "strength_display_label",
]
