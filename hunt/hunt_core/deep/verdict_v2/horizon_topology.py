"""Horizon topology — A/B/C relationships."""
from __future__ import annotations

from hunt_core.deep.verdict_v2._helpers import clamp01, direction_bias
from hunt_core.deep.verdict_v2.types import HorizonForecast, HorizonTopology


def classify_topology(horizons: dict[str, HorizonForecast]) -> HorizonTopology:
    a = horizons.get("A")
    b = horizons.get("B")
    c = horizons.get("C")
    a_dom = direction_bias(a.dominant if a else "neutral")
    b_dom = direction_bias(b.dominant if b else "neutral")
    c_dom = direction_bias(c.dominant if c else "neutral")
    a_raw = a.dominant if a else "neutral"
    b_raw = b.dominant if b else "neutral"
    c_raw = c.dominant if c else "neutral"
    evidence: list[str] = []

    if a_dom == b_dom == c_dom and a_dom != "neutral":
        kind = "aligned_trend"
        evidence.append(f"abc_{a_dom}")
    elif a_dom == "long" and b_dom in {"neutral", "short"}:
        kind = "bull_pullback"
        evidence.append("a_long_b_soft")
    elif a_dom == "short" and b_dom in {"neutral", "long"}:
        kind = "bear_rally"
        evidence.append("a_short_b_soft")
    elif a and b and a.conviction < 0.12 and b.conviction < 0.12:
        kind = "compression"
        evidence.append("low_conv_ab")
    elif a_dom != "neutral" and c_dom != "neutral" and a_dom != c_dom:
        kind = "reversal_candidate"
        evidence.append(f"a_{a_dom}_c_{c_dom}")
    else:
        kind = "mixed"
        evidence.append("mixed_horizons")

    aligned = sum(1 for d in (a_dom, b_dom, c_dom) if d == b_dom and d != "neutral")
    coherence = clamp01(0.35 + aligned * 0.2 + (b.conviction if b else 0) * 0.3)
    return HorizonTopology(
        kind=kind,  # type: ignore[arg-type]
        a_dominant=a_raw,
        b_dominant=b_raw,
        c_dominant=c_raw,
        coherence=round(coherence, 3),
        evidence=evidence,
    )
