"""Setup catalog runner — all detectors with regime conflict suppression (C.1.2)."""
from __future__ import annotations



import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from hunt_core.domain.regime_classifier import Regime, RegimeResult, regime_conflicts_direction

Direction = Literal["short", "long"]

_SCORE_CAP = 0.95

# Regime → directional prior (logit offset).
_REGIME_PRIOR: dict[str, dict[str, float]] = {
    Regime.TREND_UP.value: {"long": 0.35, "short": -0.45},
    Regime.TREND_DOWN.value: {"long": -0.40, "short": 0.30},
    Regime.RANGE.value: {"long": 0.0, "short": 0.0},
    Regime.SQUEEZE.value: {"long": -0.15, "short": -0.05},
    Regime.EXPANSION.value: {"long": 0.10, "short": 0.10},
    Regime.CAPITULATION.value: {"long": 0.25, "short": -0.35},
    Regime.EUPHORIA.value: {"long": -0.40, "short": 0.20},
}

# Setup-specific calibration intercepts.
_SETUP_INTERCEPT: dict[str, float] = {
    "dump_initiation": -0.15,
    "squeeze_expansion": -0.10,
    "liquidity_sweep": -0.05,
    "bos_choch": -0.12,
    "value_accept_reject": -0.08,
    "oi_cascade": -0.18,
    "accumulation_breakout": -0.14,
    "cex_pump": -0.06,
    "cex_dump": -0.08,
    "btc_decoupled": -0.06,
}


@dataclass(frozen=True, slots=True)
class SetupEvidence:
    """Closed-bar confirmed setup candidate from the hunt setup catalog."""

    setup_id: str
    direction: Direction
    strength: float  # 0..1 detector-native strength
    confirmed: bool
    reasons: tuple[str, ...] = ()
    entry: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def closed_tf(tf: dict[str, Any], key: str) -> dict[str, Any]:
    block = tf.get(key)
    return block if isinstance(block, dict) else {}


def closed_bar_close(tf_block: dict[str, Any]) -> float:
    if not tf_block.get("closed_bar"):
        return 0.0
    candle = tf_block.get("candle") or {}
    if isinstance(candle, dict) and candle.get("close") is not None:
        return _f(candle.get("close"))
    return _f(tf_block.get("close"))


def require_closed_bar(tf: dict[str, Any], key: str) -> tuple[dict[str, Any], float] | None:
    """Return (block, close) when the timeframe has a grace-closed bar."""
    block = closed_tf(tf, key)
    if not block.get("closed_bar"):
        return None
    close = closed_bar_close(block)
    if close <= 0:
        return None
    return block, close


def confirm_tf_chain(tf: dict[str, Any], *keys: str) -> tuple[dict[str, Any], float] | None:
    """Prefer first available closed bar from *keys* (confirm path order)."""
    for key in keys:
        hit = require_closed_bar(tf, key)
        if hit is not None:
            return hit
    return None


def atr_from_tf(tf: dict[str, Any], key: str = "15m_closed") -> float:
    block = closed_tf(tf, key) or closed_tf(tf, key.replace("_closed", ""))
    return _f(block.get("atr14"))


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _regime_label(regime: RegimeResult | dict[str, Any] | str | None) -> str:
    if regime is None:
        return Regime.RANGE.value
    if isinstance(regime, RegimeResult):
        return regime.regime.value
    if isinstance(regime, dict):
        return str(
            regime.get("regime")
            or regime.get("label")
            or regime.get("market_regime")
            or Regime.RANGE.value
        )
    return str(regime)


def _lake_adjustment(lake_stats: dict[str, Any] | None, *, setup_id: str, direction: str) -> float:
    if not lake_stats:
        return 0.0
    sym_stats = lake_stats.get("by_setup") or lake_stats.get("setups") or {}
    row = sym_stats.get(setup_id) or sym_stats.get(f"{setup_id}:{direction}") or {}
    if not isinstance(row, dict):
        return 0.0
    n = int(row.get("n") or row.get("n_closed") or 0)
    if n < 8:
        return 0.0
    wr = float(row.get("wr_pct") or row.get("win_rate") or 50.0) / 100.0
    sl_rate = float(row.get("sl_rate") or row.get("sl_hit_rate") or 0.5)
    return (wr - 0.5) * 0.8 - max(0.0, sl_rate - 0.30) * 0.6


def setup_lake_outcome_n(
    lake_stats: dict[str, Any] | None,
    *,
    setup_id: str,
    direction: str,
) -> int:
    """Closed deduped outcomes for a setup×direction (lake calibration flip threshold)."""
    if not lake_stats:
        return 0
    sym_stats = lake_stats.get("by_setup") or lake_stats.get("setups") or {}
    row = (
        sym_stats.get(f"{setup_id}:{direction}")
        or sym_stats.get(setup_id)
        or {}
    )
    if not isinstance(row, dict):
        return 0
    return int(row.get("n") or row.get("n_closed") or 0)


def _ev_bootstrap_deliver_enabled() -> bool:
    """Bootstrap: deliver EV-primary setups while still uncalibrated (default on).

    Without this the engine deadlocks — a setup needs >=min_n lake outcomes to flip
    to delivery, but cannot accumulate outcomes without delivering. During bootstrap
    the EV/P(win) floors govern quality (lake_adjustment is 0 until n>=min_n, so P is
    the first-principles prior); once n>=min_n calibration refines it. Disable with
    HUNT_EV_BOOTSTRAP=0.
    """
    return os.environ.get("HUNT_EV_BOOTSTRAP", "0").strip().lower() in {"1", "true", "yes"}


def setup_ev_flip_eligible(
    lake_stats: dict[str, Any] | None,
    *,
    setup_id: str,
    direction: str,
    min_n: int = 8,
) -> bool:
    """Per-setup EV-primary flip: calibrated (n>=min_n) OR bootstrap exploration."""
    n = setup_lake_outcome_n(lake_stats, setup_id=setup_id, direction=direction)
    if n >= min_n:
        return True
    return _ev_bootstrap_deliver_enabled()


_MAP_CONFLUENCE_CAP = 0.6


def map_confluence_logit(market: dict[str, Any] | None, direction: str) -> float:
    """Logit adjustment from professional-map confluence for a setup direction.

    Long setups are lifted by accumulation, short-squeeze fuel, ask-thinning/void above and
    bullish CVD divergence — and penalised by long-squeeze (dump) risk. Short setups mirror it.
    Bounded to ±:data:`_MAP_CONFLUENCE_CAP` so maps tilt, never dominate, the calibrated score.
    """
    if not isinstance(market, dict):
        return 0.0

    def fval(key: str) -> float | None:
        try:
            v = market.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    acc = fval("map_accumulation_score")
    fuel_short = fval("liq_squeeze_fuel_short")  # short-squeeze → pump (long fuel)
    fuel_long = fval("liq_squeeze_fuel_long")    # long-squeeze → dump (short fuel)
    cvd = market.get("map_cvd_divergence")
    score = 0.0
    if direction == "long":
        if acc is not None and acc >= 0.5:
            score += 0.5 * acc
        if fuel_short is not None and fuel_short >= 0.5:
            score += 0.5 * fuel_short
        if market.get("map_ask_thinning"):
            score += 0.12
        if cvd == "bullish_div":
            score += 0.15
        if fuel_long is not None and fuel_long >= 0.6:
            score -= 0.4 * fuel_long
        if cvd == "bearish_div":
            score -= 0.15
    elif direction == "short":
        if fuel_long is not None and fuel_long >= 0.5:
            score += 0.5 * fuel_long
        if cvd == "bearish_div":
            score += 0.15
        if acc is not None and acc >= 0.6:
            score -= 0.4 * acc
        if fuel_short is not None and fuel_short >= 0.6:
            score -= 0.4 * fuel_short
        if cvd == "bullish_div":
            score -= 0.15
    return round(max(-_MAP_CONFLUENCE_CAP, min(_MAP_CONFLUENCE_CAP, score)), 4)


def score_setup_probability(
    evidence: SetupEvidence,
    regime: RegimeResult | dict[str, Any] | str | None,
    lake_stats: dict[str, Any] | None = None,
    *,
    market: dict[str, Any] | None = None,
) -> float:
    """Return calibrated probability in [0, 0.95] for a closed-bar setup."""
    label = _regime_label(regime)
    direction = evidence.direction
    intercept = _SETUP_INTERCEPT.get(evidence.setup_id, -0.10)
    prior = (_REGIME_PRIOR.get(label) or {}).get(direction, 0.0)
    regime_conf = 0.0
    if isinstance(regime, RegimeResult):
        regime_conf = (regime.confidence - 0.5) * 0.6

    strength_logit = (evidence.strength - 0.5) * 3.2
    reason_bonus = min(0.25, len(evidence.reasons) * 0.04)
    lake_adj = _lake_adjustment(lake_stats, setup_id=evidence.setup_id, direction=direction)
    map_logit = map_confluence_logit(market, direction)

    z = intercept + prior + strength_logit + reason_bonus + regime_conf + lake_adj + map_logit
    if not evidence.confirmed:
        z -= 1.2

    p = _sigmoid(z)
    return round(min(_SCORE_CAP, max(0.0, p)), 4)


HUNT_SETUP_IDS: tuple[str, ...] = (
    "dump_initiation",
    "squeeze_expansion",
    "liquidity_sweep",
    "bos_choch",
    "value_accept_reject",
    "oi_cascade",
    "accumulation_breakout",
    "cex_pump",
    "cex_dump",
    "btc_decoupled",
)


@dataclass(frozen=True, slots=True)
class HuntSetupMeta:
    setup_id: str
    trigger_tf: str
    pattern_tf: str
    order_type: str = "limit"
    ttl_minutes: int = 120


# Catalog setup_id → structural setup_type for delivery gate (no_setup_type).
_CATALOG_SETUP_TYPE: dict[str, str] = {
    "bos_choch": "bos_retest",
    "dump_initiation": "bos_retest",
    "accumulation_breakout": "bos_retest",
    "liquidity_sweep": "sweep_reclaim",
    "value_accept_reject": "sweep_reclaim",
    "oi_cascade": "bos_retest",
    "squeeze_expansion": "bos_retest",
    "cex_pump": "bos_retest",
    "cex_dump": "bos_retest",
    "btc_decoupled": "bos_retest",
}


def catalog_struct_setup_type(setup_id: str | None) -> str | None:
    sid = str(setup_id or "").strip()
    if not sid:
        return None
    return _CATALOG_SETUP_TYPE.get(sid)


HUNT_SETUP_META: dict[str, HuntSetupMeta] = {
    "dump_initiation": HuntSetupMeta("dump_initiation", "5m", "15m", "limit", 90),
    "squeeze_expansion": HuntSetupMeta("squeeze_expansion", "15m", "15m", "limit", 120),
    "liquidity_sweep": HuntSetupMeta("liquidity_sweep", "15m", "15m", "limit", 120),
    "bos_choch": HuntSetupMeta("bos_choch", "15m", "15m", "limit", 120),
    "value_accept_reject": HuntSetupMeta("value_accept_reject", "15m", "15m", "limit", 120),
    "oi_cascade": HuntSetupMeta("oi_cascade", "5m", "15m", "limit", 90),
    "accumulation_breakout": HuntSetupMeta("accumulation_breakout", "15m", "1h", "limit", 180),
    "cex_pump": HuntSetupMeta("cex_pump", "1m", "15m", "limit", 45),
    "cex_dump": HuntSetupMeta("cex_dump", "1m", "15m", "limit", 45),
    "btc_decoupled": HuntSetupMeta("btc_decoupled", "5m", "15m", "limit", 90),
}


def resolve_setup_order_type(setup_id: str, *, default: str = "limit") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.order_type if meta else default


def resolve_setup_ttl_minutes(setup_id: str, *, default: int = 120) -> int:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.ttl_minutes if meta else default


def resolve_setup_trigger_tf(setup_id: str, *, default: str = "15m") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.trigger_tf if meta else default


def resolve_setup_pattern_tf(setup_id: str, *, default: str = "15m") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.pattern_tf if meta else default


DetectorFn = Callable[[dict[str, Any], dict[str, Any]], SetupEvidence | None]


def _catalog_detectors() -> tuple[DetectorFn, ...]:
    from hunt_core.scanner.setups.detectors import (
        detect_accumulation_breakout,
        detect_bos_choch,
        detect_btc_decoupled,
        detect_cex_dump,
        detect_cex_pump,
        detect_dump_initiation,
        detect_liquidity_sweep,
        detect_oi_cascade,
        detect_squeeze_expansion,
        detect_value_accept_reject,
    )

    return (
        detect_dump_initiation,
        detect_squeeze_expansion,
        detect_liquidity_sweep,
        detect_bos_choch,
        detect_value_accept_reject,
        detect_oi_cascade,
        detect_accumulation_breakout,
        detect_btc_decoupled,
        detect_cex_pump,
        detect_cex_dump,
    )


def resolve_catalog_regime(
    row: dict[str, Any],
    prepared: dict[str, Any] | None = None,
) -> str:
    """Structural regime for C.1.2 suppression — lifecycle.regime, not market snapshot."""
    prep = prepared if isinstance(prepared, dict) else row
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    if not lc and isinstance(prep.get("lifecycle"), dict):
        lc = prep["lifecycle"]
    regime = lc.get("regime")
    if regime:
        return str(regime)
    market = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    return _regime_label(market)


def run_setup_catalog(
    row: dict[str, Any],
    prepared: dict[str, Any],
    regime: RegimeResult | dict[str, Any] | str | None = None,
    *,
    include_forming: bool = False,
) -> list[SetupEvidence]:
    """Run all setup detectors; suppress counter-context hits per regime (C.1.2)."""
    label = _regime_label(regime) if regime is not None else resolve_catalog_regime(row, prepared)
    hits: list[SetupEvidence] = []
    for detect in _catalog_detectors():
        evidence = detect(row, prepared)
        if evidence is None:
            continue
        if not evidence.confirmed:
            if not include_forming or evidence.strength < 0.40:
                continue
        if regime_conflicts_direction(label, evidence.direction):
            continue
        prob = score_setup_probability(
            evidence,
            regime=label,
            market=row.get("market") if isinstance(row.get("market"), dict) else None,
        )
        hits.append(
            SetupEvidence(
                setup_id=evidence.setup_id,
                direction=evidence.direction,
                strength=evidence.strength,
                confirmed=evidence.confirmed,
                reasons=evidence.reasons,
                entry=evidence.entry,
                stop_loss=evidence.stop_loss,
                tp1=evidence.tp1,
                tp2=evidence.tp2,
                metadata={**evidence.metadata, "probability": round(prob, 4)},
            )
        )
    return hits


def _evidence_levels(evidence: SetupEvidence, price: float, *, atr: float = 0.0) -> dict[str, Any]:
    entry = evidence.entry if evidence.entry > 0 else price
    atr_pad = max(atr * 0.35, 0.0) if atr > 0 else 0.0
    pct_pad = entry * 0.002 if entry > 0 else price * 0.002
    pad = max(atr_pad, pct_pad, 1e-8)
    return {
        "setup_id": evidence.setup_id,
        "entry_zone": [round(entry - pad, 6), round(entry + pad, 6)],
        "stop_loss": round(evidence.stop_loss, 6),
        "tp1": round(evidence.tp1, 6),
        "tp2": round(evidence.tp2, 6),
    }


def run_setup_detectors(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run catalog detectors and attach calibrated P + EV (Phase 3B shadow)."""
    from hunt_core.contract import compute_setup_ev

    regime = resolve_catalog_regime(row, prepared)
    structure = prepared.get("structure") if isinstance(prepared.get("structure"), dict) else {}
    price = float(row.get("price") or prepared.get("price") or 0)
    market = row.get("market") if isinstance(row.get("market"), dict) else None
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    out: list[dict[str, Any]] = []
    for detect in _catalog_detectors():
        evidence = detect(row, prepared)
        if evidence is None:
            continue
        p_win = score_setup_probability(evidence, regime=regime, market=market)
        levels = _evidence_levels(evidence, price, atr=atr_from_tf(tf))
        ev_detail = compute_setup_ev(
            {
                "setup_id": evidence.setup_id,
                "direction": evidence.direction,
                "strength": evidence.strength,
                "p_win": p_win,
                "probability": p_win,
                "reasons": evidence.reasons,
            },
            levels,
            direction=evidence.direction,
            structure=structure,
        )
        out.append(
            {
                "setup_id": evidence.setup_id,
                "direction": evidence.direction,
                "strength": evidence.strength,
                "confirmed": evidence.confirmed,
                "reasons": evidence.reasons,
                "p_win": p_win,
                "ev": ev_detail.get("ev"),
                "ev_detail": ev_detail,
                "levels": levels,
            }
        )
    return out


def pick_max_ev_candidate(
    candidates: list[dict[str, Any]],
    direction: Direction,
) -> dict[str, Any] | None:
    """Return highest-EV catalog candidate for a direction."""
    pool = [
        c
        for c in candidates
        if c.get("direction") == direction and c.get("ev") is not None
    ]
    if not pool:
        return None
    return max(pool, key=lambda item: float(item["ev"]))


def legacy_fuel_merge_enabled() -> bool:
    """Legacy fuel side-boost path — off when ev_primary_default (config) or env default."""
    env = os.environ.get("HUNT_LEGACY_FUEL")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes"}
    from hunt_core.params.store import universal_section

    dl = universal_section("delivery")
    return not bool(dl.get("ev_primary_default", True))


def promote_catalog_ev_setup(
    setup: dict[str, Any],
    direction: Direction,
    ev_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Promote best catalog EV candidate — lab lane only under E1 (replaces merge_* fuel boost)."""
    if ev_candidate is None:
        return setup
    sid = str(ev_candidate.get("setup_id") or "")
    lake = ev_candidate.get("lake_stats") if isinstance(ev_candidate.get("lake_stats"), dict) else None
    if lake is None:
        try:
            from hunt_core.params.store import load_calibration

            cal = load_calibration()
            oc = cal.get("outcome_calibration") or {}
            by_setup = oc.get("by_setup") if isinstance(oc.get("by_setup"), dict) else oc
            if isinstance(by_setup, dict):
                lake = {"by_setup": by_setup}
        except Exception:
            lake = None
    try:
        ev = float(ev_candidate.get("ev"))
    except (TypeError, ValueError):
        return setup
    if ev <= 0:
        return setup
    out = apply_ev_primary_setup(setup, direction, ev_candidate)
    flip = sid and lake and setup_ev_flip_eligible(lake, setup_id=sid, direction=direction)
    if sid and lake and not flip:
        out["ev_shadow_only"] = True
    return out


def apply_ev_primary_setup(
    setup: dict[str, Any],
    direction: Direction,
    ev_candidate: dict[str, Any],
) -> dict[str, Any]:
    """Promote catalog EV-primary candidate over fuel merge_* injection."""
    out = dict(setup)
    sid = str(ev_candidate.get("setup_id") or "")
    levels = ev_candidate.get("levels") if isinstance(ev_candidate.get("levels"), dict) else {}
    strength = float(ev_candidate.get("strength") or 0)
    score_key = "dump_score" if direction == "short" else "long_score"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    catalog_boost = round(38.0 + strength * 42.0, 1)
    out[score_key] = max(float(out.get(score_key) or 0), catalog_boost)
    out[fuel_key] = max(float(out.get(fuel_key) or 0), catalog_boost)
    out["catalog_setup"] = sid
    out["catalog_strength"] = round(strength, 3)
    out["ev_primary"] = True
    out["setup_id"] = sid
    struct_type = catalog_struct_setup_type(sid)
    if struct_type:
        out["setup_type"] = struct_type
    out["phase"] = sid
    if ev_candidate.get("p_win") is not None:
        out["p_win"] = ev_candidate.get("p_win")
        out["catalog_p_win"] = ev_candidate.get("p_win")
        out["delivery_p_win"] = ev_candidate.get("p_win")
    if ev_candidate.get("ev") is not None:
        out["ev_primary_ev"] = ev_candidate.get("ev")
    if ev_candidate.get("confirmed"):
        out["catalog_confirmed"] = True
    hard = list(out.get("confirm_hard") or [])
    for reason in ev_candidate.get("reasons") or ():
        tag = str(reason)
        if tag not in hard:
            hard.append(tag)
    out["confirm_hard"] = hard
    for key in ("entry_zone", "stop_loss", "tp1", "tp2"):
        val = levels.get(key)
        if val:
            out.setdefault(key, val)
    out["delivery_lane"] = "lab"
    out["ev_bootstrap"] = True
    return out


def sync_ev_primary_confirm(
    setup: dict[str, Any],
    *,
    direction: Direction,
    symbol: str,
) -> bool:
    """Confirm EV-primary when catalog economics qualify — lab lane only (E1/A1)."""
    if not setup.get("ev_primary"):
        return bool(setup.get("confirmed"))
    if setup.get("delivery_lane") != "lab":
        return bool(setup.get("confirmed"))
    from hunt_core.scanner.gate._ev import ev_primary_delivery_qualified

    if ev_primary_delivery_qualified(setup, direction=direction, symbol=symbol):
        setup["confirmed"] = True
        setup["catalog_confirmed"] = True
        return True
    if setup.get("catalog_confirmed") and float(setup.get("catalog_strength") or 0) >= 0.55:
        setup["confirmed"] = True
        return True
    return bool(setup.get("confirmed"))


def build_ev_delivery_eval(
    result: dict[str, Any],
    *,
    symbol: str,
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    """Telemetry mirror of live dump/long EV-primary gate (no duplicate detector pass)."""
    from hunt_core.scanner.gate._report import collect_report_blockers

    out: dict[str, Any] = {}
    for direction, key in (("short", "dump"), ("long", "long")):
        setup = result.get(key)
        if not isinstance(setup, dict):
            continue
        if not setup.get("ev_primary"):
            continue
        blockers = collect_report_blockers(
            setup,
            direction=direction,
            symbol=symbol,
            lifecycle=lifecycle,
            row=result,
        )
        out[direction] = {
            "setup_id": setup.get("catalog_setup") or setup.get("setup_id"),
            "p_win": setup.get("delivery_p_win") or setup.get("p_win"),
            "ev": setup.get("delivery_ev") or setup.get("ev_primary_ev"),
            "would_deliver": not blockers and bool(setup.get("confirmed")),
            "blockers": [b.code for b in blockers],
            "ev_shadow_only": bool(setup.get("ev_shadow_only")),
        }
    return out


__all__ = [
    "HUNT_SETUP_IDS",
    "HUNT_SETUP_META",
    "HuntSetupMeta",
    "SetupEvidence",
    "apply_ev_primary_setup",
    "atr_from_tf",
    "build_ev_delivery_eval",
    "catalog_struct_setup_type",
    "closed_bar_close",
    "closed_tf",
    "confirm_tf_chain",
    "legacy_fuel_merge_enabled",
    "pick_max_ev_candidate",
    "promote_catalog_ev_setup",
    "require_closed_bar",
    "resolve_catalog_regime",
    "resolve_setup_order_type",
    "resolve_setup_pattern_tf",
    "resolve_setup_trigger_tf",
    "resolve_setup_ttl_minutes",
    "run_setup_catalog",
    "run_setup_detectors",
    "score_setup_probability",
    "sync_ev_primary_confirm",
]
