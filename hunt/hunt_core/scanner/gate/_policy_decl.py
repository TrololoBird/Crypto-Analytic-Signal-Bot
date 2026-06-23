"""Declarative delivery gates (Phase 9 split)."""
from __future__ import annotations

import os
from typing import Any, Literal

from hunt_core.scanner.gate._rules_table import (
    DELIVERY_GATE_RULES,
    DeliveryGateTier,
)


def _decl_tier_matches(rule_tier: DeliveryGateTier, delivery_tier: str) -> bool:
    if rule_tier == "both":
        return True
    return rule_tier == delivery_tier


def _decl_snapshot_tier(row: dict[str, Any], setup: dict[str, Any]) -> str:
    from hunt_core.scanner.gate.delivery import _snapshot_tier_from_row  # noqa: PLC0415

    return _snapshot_tier_from_row(row, setup)


def _decl_check_data_complete(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.data.completeness import delivery_derivatives_complete
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = direction, lifecycle, delivery_tier
    tier = _decl_snapshot_tier(row, setup)
    ok, missing = delivery_derivatives_complete(row, tier=tier)
    if ok:
        return None
    detail = ", ".join(missing[:8])
    if len(missing) > 8:
        detail += f" (+{len(missing) - 8})"
    return GateResult(
        False,
        "data_incomplete",
        f"Деривативы неполные ({tier}): {detail}",
    )


def _decl_check_data_stale(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.data.completeness import DELIVERY_MARKET_KEYS_FAST, DELIVERY_MARKET_KEYS_FULL
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = direction, lifecycle, delivery_tier, symbol
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    max_age = float(os.getenv("HUNT_MAX_DERIVATIVE_AGE_S", "300") or 300)
    tier = _decl_snapshot_tier(row, setup)
    keys = DELIVERY_MARKET_KEYS_FAST if tier in {"fast", "hot"} else DELIVERY_MARKET_KEYS_FULL
    stale: list[str] = []
    for key in keys:
        age_raw = market.get(f"{key}_age_seconds")
        if age_raw is None:
            continue
        try:
            age_s = float(age_raw)
        except (TypeError, ValueError):
            continue
        if age_s > max_age:
            stale.append(f"{key}={age_s:.0f}s")
    if not stale:
        return None
    detail = ", ".join(stale[:6])
    if len(stale) > 6:
        detail += f" (+{len(stale) - 6})"
    return GateResult(
        False,
        "data_stale",
        f"Деривативы устарели (>{max_age:.0f}s): {detail}",
    )


def _structure_opposes_direction(bias: str, direction: str) -> bool:
    b = bias.lower().strip()
    d = direction.lower().strip()
    bearish = b in {"bear", "short", "down", "downtrend", "bearish"}
    bullish = b in {"bull", "long", "up", "uptrend", "bullish"}
    if d == "long" and bearish:
        return True
    if d == "short" and bullish:
        return True
    return False


def _structure_is_choch(struct: dict[str, Any]) -> bool:
    if bool(struct.get("choch")):
        return True
    event = str(struct.get("event") or struct.get("bos_choch") or "").lower()
    return "choch" in event


def _decl_check_structure_aligned(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, lifecycle, delivery_tier, symbol
    struct = row.get("structure")
    if not isinstance(struct, dict) or not struct:
        return None
    bias = str(
        struct.get("structure_bias") or struct.get("bias") or struct.get("htf_trend") or ""
    ).strip()
    if not bias or bias.lower() in {"neutral", "ranging", "range", "—", "wait"}:
        return None
    if _structure_is_choch(struct):
        return None
    if not _structure_opposes_direction(bias, direction):
        return None
    return GateResult(
        False,
        "structure_bias_conflict",
        f"Structure bias {bias} против {direction}",
    )


def _decl_check_lifecycle_context(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    """MLIVE-8: block direction vs 24h tape contradictions without CHoCH."""
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, delivery_tier, symbol
    phase = str(lifecycle.get("phase") or "")
    try:
        chg = abs(float(row.get("chg_24h_pct") or row.get("change_24h_pct") or 0))
    except (TypeError, ValueError):
        chg = 0.0
    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    choch = _structure_is_choch(struct)
    d = direction.lower().strip()
    if d == "long" and phase == "impulse_initiating" and chg >= 15.0 and not choch:
        return GateResult(
            False,
            "lifecycle_context_veto",
            f"Long impulse при −{chg:.1f}%/24h без CHoCH — knife-catch",
        )
    if d == "short" and phase in {"dump_initiating", "dump_active"} and chg < 3.0:
        return GateResult(
            False,
            "lifecycle_context_veto",
            f"Short dump при flat 24h ({chg:.1f}%) — нет импульса",
        )
    bias = str(lifecycle.get("recommended_bias") or "")
    if (
        d == "short"
        and phase == "dump_active"
        and bias == "wait"
        and not choch
        and float(setup.get("ignition_score") or 0) < float(
            os.getenv("HUNT_IGNITION_OVERRIDE", "55") or 55
        )
    ):
        return GateResult(
            False,
            "bias_wait_mid_dump",
            "Bias wait + mid-dump без CHoCH/ignition — только monitor",
        )
    return None


def _decl_check_at_level(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult, price_in_entry_zone  # noqa: PLC0415

    _ = lifecycle, delivery_tier, symbol
    struct = row.get("structure")
    if isinstance(struct, dict) and bool(struct.get("at_level")):
        return None
    price = float(row.get("price") or 0)
    if price > 0 and price_in_entry_zone(setup, price, direction=direction):
        return None
    return GateResult(
        False,
        "not_at_level",
        "Цена вне entry zone и structure.at_level не выставлен",
    )


def _decl_check_rr_floor(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.contract import compute_setup_risk_reward
    from hunt_core.scanner.gate.delivery import (  # noqa: PLC0415
        GateResult,
        _effective_min_rr,
    )
    from hunt_core.params.store import effective_hunt_params

    _ = row, delivery_tier
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    min_rr = _effective_min_rr(
        setup,
        direction=direction,
        symbol=sym,
        lc=lifecycle,
        cal=cal,
    )
    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is not None:
        setup["risk_reward"] = rr
    if rr is None:
        return GateResult(False, "rr_missing", "R:R не вычислен — нет entry/SL/TP1")
    if float(rr) < min_rr:
        return GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")
    return None


def _decl_check_playbook(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.playbook import setup_meets_playbook
    from hunt_core.scanner.gate._ev import legacy_fuel_delivery_enabled
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = lifecycle, delivery_tier, symbol
    if legacy_fuel_delivery_enabled():
        return None
    dir_lit = "short" if direction == "short" else "long"
    if setup_meets_playbook(setup, row=row, direction=dir_lit):  # type: ignore[arg-type]
        return None
    fusion = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
    pc = fusion.get("pass_count", 0)
    req = fusion.get("required_n", 0)
    arch = fusion.get("archetype") or "none"
    return GateResult(
        False,
        "playbook_fail",
        f"Playbook {pc}/{req} для {arch} — N-of-M не пройден",
    )


def _decl_check_ev_delivery(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._ev import (
        delivery_ev_floors,
        legacy_fuel_delivery_enabled,
        pwin_gate_enabled,
        resolve_delivery_ev,
    )
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.scanner.gate._delivery_helpers import (
        count_fuel_evidence,
        evidence_coverage_ratio,
    )

    _ = lifecycle, delivery_tier
    sym = symbol.upper()
    dir_lit = "short" if direction == "short" else "long"
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    present, total = count_fuel_evidence(market, direction=dir_lit)
    coverage = evidence_coverage_ratio(market, direction=dir_lit)
    setup["fuel_evidence_present"] = present
    setup["fuel_evidence_total"] = total
    setup["fuel_evidence_coverage"] = round(coverage, 3)

    if legacy_fuel_delivery_enabled():
        from hunt_core.scanner.gate._delivery_helpers import evidence_adjusted_min_fuel
        from hunt_core.scanner.gate.delivery import _setup_fuel
        from hunt_core.params.store import delivery_thresholds

        dl = delivery_thresholds(sym)
        base_min_fuel = float(dl.get("min_fuel", 72.0))
        evidence_floor = evidence_adjusted_min_fuel(base_min_fuel, coverage)
        setup["_declarative_evidence_floor"] = evidence_floor
        if evidence_floor is None:
            return GateResult(
                False,
                "fuel_evidence_sparse",
                f"Недостаточно evidence для fuel ({present}/{total}, coverage {coverage:.0%})",
            )
        fuel = _setup_fuel(setup, direction)
        if fuel < float(evidence_floor):
            return GateResult(
                False,
                "below_min_fuel",
                f"Fuel {fuel:.0f} < evidence floor {float(evidence_floor):.0f} "
                f"(coverage {coverage:.0%}, {present}/{total})",
            )
        return None

    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    resolved = resolve_delivery_ev(setup, direction=dir_lit, row=row, structure=struct)
    ev = resolved.get("ev")
    p_win = resolved.get("p_win")
    confirmed = bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))
    min_ev, min_p = delivery_ev_floors(sym, confirmed=confirmed)

    if ev is not None:
        setup["delivery_ev"] = ev
    if p_win is not None:
        setup["delivery_p_win"] = p_win
    setup["delivery_ev_source"] = resolved.get("source")

    if ev is None and p_win is None:
        return GateResult(
            False,
            "data.ev_missing",
            "EV и P(win) отсутствуют после resolve",
        )

    if ev is None:
        reason = resolved.get("reason") or "incomplete_levels"
        return GateResult(
            False,
            "ev_incomplete",
            f"EV не вычислен ({reason}) — нет entry/SL/TP1 или P",
        )
    try:
        ev_f = float(ev)
    except (TypeError, ValueError):
        return GateResult(False, "ev_incomplete", "EV не числовой")
    if ev_f <= min_ev:
        return GateResult(
            False,
            "ev_below_floor",
            f"EV {ev_f:.4f} ≤ floor {min_ev:.4f}",
        )
    if not pwin_gate_enabled():
        shadow = setup.get("ev_shadow")
        if not isinstance(shadow, dict):
            shadow = {}
            setup["ev_shadow"] = shadow
        if p_win is not None:
            shadow["p_win"] = p_win
            shadow["p_win_shadow_only"] = True
        return None
    if p_win is None:
        return GateResult(False, "p_win_missing", "P(win) не вычислен для delivery")
    try:
        p_f = float(p_win)
    except (TypeError, ValueError):
        return GateResult(False, "p_win_missing", "P(win) не числовой")
    if p_f < min_p:
        return GateResult(
            False,
            "p_win_below_floor",
            f"P(win) {p_f:.2f} < floor {min_p:.2f}",
        )
    return None


def _decl_check_structural_trigger(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult, _structural_hard_count  # noqa: PLC0415

    _ = row, lifecycle, symbol
    if delivery_tier != "triggered":
        return None
    if setup.get("intrabar_confirmed"):
        return None
    hard = setup.get("confirm_hard") or []
    struct_n = _structural_hard_count(hard, direction=direction)
    if struct_n >= 1:
        return None
    return GateResult(
        False,
        "no_structural_trigger",
        f"Нет structural trigger (hard={struct_n}, нужен ≥1)",
    )


def _decl_check_ignition_floor(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.scanner.gate._delivery_helpers import EARLY_ADVISORY_MIN_IGNITION

    _ = row, direction, lifecycle, symbol
    if delivery_tier != "armed":
        return None
    if not (
        setup.get("anticipation")
        or setup.get("early_tier") == "armed"
        or setup.get("intrabar_armed")
    ):
        return None
    ign = setup.get("ignition_score")
    if ign is None:
        return None
    try:
        ign_f = float(ign)
    except (TypeError, ValueError):
        return GateResult(False, "ignition_low", "Ignition score invalid")
    min_ign = float(os.getenv("HUNT_MIN_IGNITION_ARMED", str(EARLY_ADVISORY_MIN_IGNITION)) or EARLY_ADVISORY_MIN_IGNITION)
    if ign_f < min_ign:
        return GateResult(
            False,
            "ignition_low",
            f"Ignition {ign_f:.0f} < min {min_ign:.0f} для ARMED",
        )
    return None


def _decl_check_orderflow_present(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    """Soft optional — annotate only; never blocks delivery."""
    from hunt_core.scanner.gate._delivery_helpers import _orderflow_confirm_aligned

    _ = lifecycle, delivery_tier
    sym = symbol.upper()
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    aligned, reason = _orderflow_confirm_aligned(direction, market, symbol=sym)
    if aligned:
        setup.pop("orderflow_soft_note", None)
        return None
    if reason and market.get("agg_trade_delta_60s") is not None:
        setup["orderflow_soft_note"] = reason
    return None


def _decl_check_setup_type(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = row, direction, lifecycle, symbol
    if delivery_tier != "triggered":
        return None
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    st = setup.get("setup_type") or row.get("setup_type")
    if st is None and setup.get("ev_primary") and setup.get("catalog_setup"):
        from hunt_core.scanner.setups.catalog import catalog_struct_setup_type

        st = catalog_struct_setup_type(str(setup.get("catalog_setup")))
        if st:
            setup["setup_type"] = st
    if st is None:
        return GateResult(
            False,
            "no_setup_type",
            "Нет структурного setup_type — только monitor",
        )
    return None


def _decl_check_meme_pump_volume(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import check_meme_pump_volume_ratio  # noqa: PLC0415

    _ = delivery_tier
    return check_meme_pump_volume_ratio(
        setup,
        direction=direction,
        lifecycle=lifecycle if isinstance(lifecycle, dict) else {},
        row=row,
        symbol=symbol.upper(),
    )


def _decl_check_meme_anomaly(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import (  # noqa: PLC0415
        _row_chg24_abs,
        _row_rng24,
        meme_anomaly_block_code,
        passes_meme_anomaly_gate,
    )
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.params.store import effective_hunt_params

    _ = setup, delivery_tier
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    if passes_meme_anomaly_gate(sym=sym, row=row, lc=lc, cal=cal):
        return None
    block_code = meme_anomaly_block_code(sym=sym, row=row, lc=lc, cal=cal) or "not_anomaly"
    chg24 = _row_chg24_abs(row)
    rng24 = _row_rng24(row)
    chg_s = f"{chg24:.1f}" if chg24 is not None else "n/a"
    rng_s = f"{rng24:.1f}" if rng24 is not None else "n/a"
    return GateResult(
        False,
        block_code,
        f"Не meme-аномалия: chg24={chg_s}% range={rng_s}% "
        f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
    )


def _decl_check_delivery_confluence(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import check_delivery_confluence  # noqa: PLC0415

    _ = delivery_tier
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_delivery_confluence(
        setup,
        direction=direction,
        symbol=symbol.upper(),
        lifecycle=lifecycle,
        row=row,
    )


def _decl_check_exhaustion_fade(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import check_exhaustion_fade  # noqa: PLC0415

    _ = delivery_tier
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_exhaustion_fade(
        setup,
        direction=direction,
        symbol=symbol.upper(),
        lifecycle=lifecycle,
        row=row,
    )


def _decl_check_impulse_long(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import check_impulse_long  # noqa: PLC0415

    _ = delivery_tier
    if direction != "long" or not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_impulse_long(
        setup, lifecycle=lifecycle, row=row, symbol=symbol.upper()
    )


def _decl_check_accumulation_long(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate._quality import check_accumulation_long  # noqa: PLC0415

    _ = delivery_tier
    if direction != "long" or not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_accumulation_long(
        setup, lifecycle=lifecycle, row=row, symbol=symbol.upper()
    )


def _decl_check_wash_baseline(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, direction, lifecycle, delivery_tier, symbol
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    baseline = market.get("quote_vol_baseline")
    if baseline is None and market.get("quote_vol_history"):
        return GateResult(
            False,
            "wash_no_baseline",
            "Wash gate: quote_vol_baseline не вычислен",
        )
    return None


def _decl_check_ev_shadow(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    import os

    from hunt_core.scanner.gate.delivery import GateResult  # noqa: PLC0415

    _ = row, direction, lifecycle, symbol, delivery_tier
    flip_on = os.environ.get("HUNT_EV_FLIP", "0").strip().lower() in {"1", "true", "yes"}
    delivery_on = os.environ.get("HUNT_EV_DELIVERY", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not flip_on and not delivery_on:
        return None
    ev_block = setup.get("ev_shadow") if isinstance(setup.get("ev_shadow"), dict) else {}
    ev_val = ev_block.get("ev")
    if ev_val is None:
        return None
    try:
        ev_f = float(ev_val)
    except (TypeError, ValueError):
        return None
    if delivery_on:
        try:
            min_ev = float(os.getenv("HUNT_EV_MIN", "0") or 0)
        except (TypeError, ValueError):
            min_ev = 0.0
        if ev_f < min_ev:
            return GateResult(
                False,
                "ev_delivery_block",
                f"EV {ev_f:.3f} < floor {min_ev:.3f} (HUNT_EV_DELIVERY=1)",
            )
    if flip_on and ev_f < 0:
        return GateResult(
            False,
            "ev_shadow_negative",
            f"EV shadow {ev_f:.3f} < 0 (HUNT_EV_FLIP=1)",
        )
    return None


_DECL_CHECK_DISPATCH: dict[str, Any] = {
    "_decl_check_data_complete": _decl_check_data_complete,
    "_decl_check_data_stale": _decl_check_data_stale,
    "_decl_check_structure_aligned": _decl_check_structure_aligned,
    "_decl_check_lifecycle_context": _decl_check_lifecycle_context,
    "_decl_check_at_level": _decl_check_at_level,
    "_decl_check_rr_floor": _decl_check_rr_floor,
    "_decl_check_playbook": _decl_check_playbook,
    "_decl_check_ev_delivery": _decl_check_ev_delivery,
    "_decl_check_structural_trigger": _decl_check_structural_trigger,
    "_decl_check_ignition_floor": _decl_check_ignition_floor,
    "_decl_check_orderflow_present": _decl_check_orderflow_present,
    "_decl_check_setup_type": _decl_check_setup_type,
    "_decl_check_meme_pump_volume": _decl_check_meme_pump_volume,
    "_decl_check_meme_anomaly": _decl_check_meme_anomaly,
    "_decl_check_ev_shadow": _decl_check_ev_shadow,
    "_decl_check_delivery_confluence": _decl_check_delivery_confluence,
    "_decl_check_exhaustion_fade": _decl_check_exhaustion_fade,
    "_decl_check_impulse_long": _decl_check_impulse_long,
    "_decl_check_accumulation_long": _decl_check_accumulation_long,
    "_decl_check_wash_baseline": _decl_check_wash_baseline,
}


def run_declarative_delivery_gates(
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    *,
    tier: Literal["armed", "triggered"] = "triggered",
    symbol: str = "",
) -> Any | None:
    """Run ordered declarative gates; first failure wins."""
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    sym = symbol.upper() or str(row.get("symbol", "")).upper()
    for rule in DELIVERY_GATE_RULES:
        if not _decl_tier_matches(rule.required_for_tier, tier):
            continue
        checker = _DECL_CHECK_DISPATCH.get(rule.check_fn)
        if checker is None:
            continue
        blocked = checker(
            row=row,
            setup=setup,
            direction=direction,
            lifecycle=lc,
            delivery_tier=tier,
            symbol=sym,
        )
        if blocked is not None:
            return blocked
    return None
