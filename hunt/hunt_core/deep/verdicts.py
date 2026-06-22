"""Three equal-weight verdicts — long / short / sideways (structure-first)."""
from __future__ import annotations

from typing import Any


def _verdict_from_pinned(pv: Any) -> dict[str, Any]:
    long_s = float(getattr(pv.long_scenario, "score", 0) or 0)
    short_s = float(getattr(pv.short_scenario, "score", 0) or 0)
    kind = str(getattr(pv, "kind", "sideways") or "sideways")
    conf_base = float(getattr(pv, "confidence", 0) or 0)

    if kind == "long":
        long_raw, short_raw, side_raw = max(long_s, conf_base), short_s * 0.85, 0.35
    elif kind == "short":
        long_raw, short_raw, side_raw = long_s * 0.85, max(short_s, conf_base), 0.35
    else:
        long_raw, short_raw = long_s, short_s
        side_raw = max(0.45, 1.0 - max(long_raw, short_raw))

    scores = {"long": long_raw, "short": short_raw, "sideways": side_raw}
    dominant = max(scores, key=scores.get)  # type: ignore[arg-type]
    gap = max(scores.values()) - sorted(scores.values())[-2] if len(scores) >= 2 else 0.0
    if gap < 0.10 and kind == "sideways":
        dominant = "sideways"

    def _pack(raw: float) -> dict[str, Any]:
        return {
            "score": round(min(1.0, max(0.0, raw)), 3),
            "confidence": round(min(1.0, max(0.0, raw + 0.12)), 3),
        }

    return {
        "long": _pack(long_raw),
        "short": _pack(short_raw),
        "sideways": _pack(side_raw),
        "dominant": dominant,
        "gap": round(gap, 3),
        "source": "pinned_verdict",
        "reason": str(getattr(pv, "reason", "") or ""),
    }


def _verdict_from_mtf(row: dict[str, Any]) -> dict[str, Any] | None:
    mtf = row.get("mtf")
    if mtf is None:
        return None
    long_s = float(getattr(getattr(mtf, "long_scenario", None), "score", 0) or 0)
    short_s = float(getattr(getattr(mtf, "short_scenario", None), "score", 0) or 0)
    dom = str(getattr(mtf, "dominant", "") or "")
    if dom not in {"long", "short", "neutral"}:
        dom = "sideways" if abs(long_s - short_s) < 0.12 else ("long" if long_s > short_s else "short")
    if dom == "neutral":
        dom = "sideways"
    side_raw = max(0.35, 1.0 - max(long_s, short_s))

    def _pack(raw: float) -> dict[str, Any]:
        return {"score": round(raw, 3), "confidence": round(min(1.0, raw + 0.12), 3)}

    scores = {"long": long_s, "short": short_s, "sideways": side_raw}
    return {
        "long": _pack(long_s),
        "short": _pack(short_s),
        "sideways": _pack(side_raw),
        "dominant": dom,
        "gap": round(max(scores.values()) - sorted(scores.values())[-2], 3),
        "source": "mtf",
    }


def _verdict_from_v2(v2: Any) -> dict[str, Any]:
    dec = v2.signal_decision
    path = v2.expected_path
    h_b = v2.horizons.get("B")
    long_raw = float(h_b.long) if h_b else 0.5
    short_raw = float(h_b.short) if h_b else 0.5
    side_raw = float(h_b.range_probability) if h_b else max(0.35, 1.0 - max(long_raw, short_raw))

    action = str(dec.action or "wait")
    if action == "long":
        long_raw = max(long_raw, v2.signal_strength.score)
    elif action == "short":
        short_raw = max(short_raw, v2.signal_strength.score)
    else:
        side_raw = max(side_raw, 1.0 - v2.signal_strength.score)

    scores = {"long": long_raw, "short": short_raw, "sideways": side_raw}
    # Dominant = highest-scoring verdict so the panel is internally consistent.
    # A WAIT action does NOT force "sideways": the directional lean (e.g. short 0.68)
    # is still shown as dominant; the WAIT/gates line communicates "don't trade yet".
    dominant = max(scores, key=scores.get)  # type: ignore[arg-type]
    gap = max(scores.values()) - sorted(scores.values())[-2]

    def _pack(raw: float) -> dict[str, Any]:
        return {
            "score": round(min(1.0, max(0.0, raw)), 3),
            "confidence": round(min(1.0, max(0.0, raw + 0.12)), 3),
        }

    return {
        "long": _pack(long_raw),
        "short": _pack(short_raw),
        "sideways": _pack(side_raw),
        "dominant": dominant,
        "gap": round(gap, 3),
        "source": "verdict_v2",
        "reason": str(dec.reason or ""),
        "path": str(path.type or ""),
        "action": action,
    }


def build_three_verdicts(
    row: dict[str, Any],
    *,
    fusion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structure-first verdict panel — verdict_v2 / pinned / MTF fallback."""
    v2 = row.get("verdict_v2")
    if v2 is not None:
        return _verdict_from_v2(v2)

    pv = row.get("pinned_verdict")
    if pv is not None:
        return _verdict_from_pinned(pv)

    sym = str(row.get("symbol") or "").upper()
    tf = row.get("timeframes") or {}
    work = row
    if sym and tf and not row.get("mtf"):
        from hunt_core.confluence.mtf import build_mtf_confluence

        price = float(row.get("price") or 0)
        if price > 0:
            work = dict(row)
            work["mtf"] = build_mtf_confluence(
                sym,
                tf,
                price,
                market=row.get("market"),
                row=row,
            )
            mtf_verdict = _verdict_from_mtf(work)
            if mtf_verdict:
                return mtf_verdict

    if sym and tf:
        from hunt_core.deep.pinned import build_pinned_verdict

        try:
            built = build_pinned_verdict(dict(row))
            return _verdict_from_pinned(built)
        except Exception:
            pass

    mtf_verdict = _verdict_from_mtf(row)
    if mtf_verdict:
        return mtf_verdict

    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    bias = str(structure.get("structure_bias") or "wait")
    long_score = 0.55 if bias == "long" else 0.35
    short_score = 0.55 if bias == "short" else 0.35
    sideways_score = 0.50 if bias == "wait" else max(0.0, 1.0 - max(long_score, short_score))
    scores = {"long": long_score, "short": short_score, "sideways": sideways_score}
    dominant = max(scores, key=scores.get)  # type: ignore[arg-type]

    def _pack(raw: float) -> dict[str, Any]:
        return {"score": round(raw, 3), "confidence": round(min(1.0, raw + 0.15), 3)}

    return {
        "long": _pack(long_score),
        "short": _pack(short_score),
        "sideways": _pack(sideways_score),
        "dominant": dominant,
        "gap": round(max(scores.values()) - sorted(scores.values())[-2], 3),
        "source": "structure_bias",
    }


__all__ = ["build_three_verdicts"]
