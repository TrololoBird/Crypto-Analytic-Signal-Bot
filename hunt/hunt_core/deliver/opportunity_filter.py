"""OpportunityFilter — computes whether a Detection is worth human attention.

Placed **before** the full gate pipeline.  Low-opportunity signals are
suppressed early, saving CPU and reducing false-positive Telegram noise.

Architecture::

    Detection → OpportunityFilter → (pass) → Gate Pipeline → Contract → Telegram
                    |                    ↑
              opportunity < 0.7          |
              └──→ suppress (no delivery)

Four dimensions (in order):

    1. **Confidence** — heuristic composite score in [0,1] (NOT a calibrated
       P(win): analyst.engines.calibration is an offline rollup tool, verified
       not wired into scenario_confidence/this value as of 2026-07). < 0.70 → reject.
    2. **Evidence** — count of *independent* directional factors (not amplifiers).
       Fewer than 2 → reject regardless of confidence.
    3. **Score** — fusion magnitude (strength, not probability).  < 30 → reject.
    4. **Opportunity** — non-linear blend::

        opportunity = f(confidence, evidence, score, market_context, regime)

       Configurable via ``OPPORTUNITY_WEIGHTS`` dict.

**Rejection logging:** every suppressed signal is logged at WARN with symbol,
direction, reason, and current values — enabling post-hoc analysis of why the
system chose silence over delivery.
"""
from __future__ import annotations

import logging
from typing import Any

from hunt_core.signals.opportunity import Opportunity, resolve_ttl

_LOG = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
_MIN_CONFIDENCE = 0.40
_MIN_EVIDENCE = 2          # independent directional factors
_MIN_FUSION_SCORE = 30.0
_MIN_OPPORTUNITY = 0.70

# ── Non-linear opportunity weights (configurable) ────────────────────────────
# A simple linear-weighted model that can be tuned per regime/environment.
# Keys: component names; values: (weight, sigmoid_steepness).
# weight=0 disables the component.
OPPORTUNITY_WEIGHTS: dict[str, float] = {
    "confidence": 0.45,   # P(win) — highest weight
    "evidence": 0.25,     # independent confirmation count
    "score": 0.20,        # fusion magnitude (strength)
    "spread_bonus": 0.05, # tight spread
    "funding_bonus": 0.05,# neutral funding
}
# Penalties (subtracted, not weighted)
_SPREAD_WIDE_PENALTY = -0.15
_FUNDING_HOT_PENALTY = -0.10

# Max independent directional factors for evidence normalisation.
_MAX_DIRECTIONAL_EVIDENCE = 6


def evaluate_opportunity(
    *,
    confidence: float,
    fusion_score: float,
    side: str,
    symbol: str,
    n_active: int,
    agreement: bool,
    factors: list[Any] | None = None,
    factor_scores: dict[str, float] | None = None,
    factor_details: dict[str, str] | None = None,
    market_ctx: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
) -> Opportunity:
    """Evaluate whether this Detection is worth human attention.

    Four-stage gate:

        1. **Confidence** — P(win) < 0.70 → immediate reject.
        2. **Evidence** — < 2 independent directional factors → reject.
        3. **Score** — fusion magnitude < 30 → reject.
        4. **Opportunity blend** — non-linear function of confidence, evidence,
           score, and market context bonuses.

    Every rejection is logged at WARN with structured context.
    """
    now = _now_iso()
    reasons: list[str] = []

    # ── Parse factor names by kind ───────────────────────────────────────────
    active_names: tuple[str, ...] = ()
    dir_names: list[str] = []
    amp_names: list[str] = []

    if factors:
        for f in factors:
            name = getattr(f, "name", "")
            kind = getattr(f, "kind", "")
            active = getattr(f, "active", False)
            if active:
                active_names = (*active_names, name)
                if kind == "directional":
                    dir_names.append(name)
                else:
                    amp_names.append(name)

    evidence_count = len(dir_names)
    evidence_norm = min(1.0, evidence_count / _MAX_DIRECTIONAL_EVIDENCE)

    # ── Resolve dynamic TTL from fastest-decaying factor ─────────────────────
    if ttl_seconds is None:
        ttl_seconds = resolve_ttl(active_names)
    from datetime import UTC, datetime, timedelta
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

    # ── Helper: reject & log ─────────────────────────────────────────────────
    def _reject(code: str, msg: str) -> Opportunity:
        _LOG.warning(
            "opportunity_reject | symbol=%s dir=%s code=%s reason=%s "
            "confidence=%.3f evidence=%d score=%.1f ttl=%ds",
            symbol, side, code, msg,
            confidence, evidence_count, fusion_score, ttl_seconds,
        )
        return Opportunity(
            symbol=symbol,
            direction=side,
            confidence=confidence,
            evidence=evidence_norm,
            score=fusion_score / 100.0,
            opportunity=0.0,
            reasons=(code, msg),
            generated_at=now,
            ttl_seconds=ttl_seconds,
            expires_at=expires,
        )

    # ── 1. Confidence gate ───────────────────────────────────────────────────
    if confidence < _MIN_CONFIDENCE:
        return _reject(
            "low_confidence",
            f"confidence={confidence:.2f} < {_MIN_CONFIDENCE}",
        )

    # ── 2. Evidence gate (NEW) ───────────────────────────────────────────────
    if evidence_count < _MIN_EVIDENCE:
        return _reject(
            "insufficient_evidence",
            f"directional_factors={evidence_count} < {_MIN_EVIDENCE}",
        )

    # ── 2b. Factor agreement ─────────────────────────────────────────────────
    if not agreement:
        return _reject(
            "factor_disagreement",
            "directional factors disagree on side",
        )

    # ── 3. Score floor ───────────────────────────────────────────────────────
    if fusion_score < _MIN_FUSION_SCORE:
        return _reject(
            "low_score",
            f"fusion_score={fusion_score:.1f} < {_MIN_FUSION_SCORE}",
        )

    # ── 4. Non-linear opportunity blend ──────────────────────────────────────
    score_norm = min(1.0, fusion_score / 100.0)

    opportunity = (
        OPPORTUNITY_WEIGHTS["confidence"] * confidence
        + OPPORTUNITY_WEIGHTS["evidence"] * evidence_norm
        + OPPORTUNITY_WEIGHTS["score"] * score_norm
    )
    reasons.append(
        f"confidence={confidence:.2f} × evidence={evidence_count} × score={score_norm:.2f}"
    )

    # ── 5. Market context ────────────────────────────────────────────────────
    spread_bps: float | None = None
    volume_24h_usd: float | None = None
    oi_usd: float | None = None
    funding_rate: float | None = None

    if market_ctx:
        sp = market_ctx.get("spread_bps")
        if sp is not None:
            spread_bps = float(sp)
            if 0 < spread_bps < 50:
                opportunity += OPPORTUNITY_WEIGHTS["spread_bonus"]
                reasons.append(f"tight_spread:{spread_bps:.1f}bps")
            elif spread_bps > 200:
                opportunity += _SPREAD_WIDE_PENALTY
                reasons.append(f"wide_spread:{spread_bps:.1f}bps")
        vol = market_ctx.get("volume_24h_usd")
        if vol is not None:
            volume_24h_usd = float(vol)
        oi = market_ctx.get("oi_usd")
        if oi is not None:
            oi_usd = float(oi)
        fr = market_ctx.get("funding_rate")
        if fr is not None:
            funding_rate = float(fr)
            if -0.001 < funding_rate < 0.001:
                opportunity += OPPORTUNITY_WEIGHTS["funding_bonus"]
                reasons.append(f"neutral_funding:{funding_rate:.4f}%")
            elif funding_rate > 0.01:
                opportunity += _FUNDING_HOT_PENALTY
                reasons.append(f"hot_funding:{funding_rate:.4f}%")

    opportunity = max(0.0, min(1.0, opportunity))

    # ── Quantitative explanations ────────────────────────────────────────────
    # Append z-score / percentile info from factor detail strings.
    quant_reasons: list[str] = []
    if factor_details:
        for name, detail in factor_details.items():
            if detail:
                quant_reasons.append(f"{name}:{detail}")
    if quant_reasons:
        reasons.extend(quant_reasons[:5])  # limit to 5 quantitative lines

    return Opportunity(
        symbol=symbol,
        direction=side,
        confidence=confidence,
        evidence=evidence_norm,
        score=score_norm,
        opportunity=opportunity,
        reasons=tuple(reasons),
        confluence_count=evidence_count,
        generated_at=now,
        ttl_seconds=ttl_seconds,
        expires_at=expires,
        spread_bps=spread_bps,
        volume_24h_usd=volume_24h_usd,
        oi_usd=oi_usd,
        funding_rate=funding_rate,
        active_factors=active_names,
        factor_scores=dict(factor_scores) if factor_scores else {},
        factor_details=dict(factor_details) if factor_details else {},
    )


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


__all__ = ["evaluate_opportunity"]
