"""Quality-weighted horizon blends."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2._helpers import clamp01, conviction, dominant_side
from hunt_core.analysis.deep.verdict_v2.config import VerdictV2Config
from hunt_core.analysis.deep.verdict_v2.types import EngineOutput, HorizonForecast, HorizonKey


def _blend(
    engines: dict[str, EngineOutput],
    priorities: dict[str, float],
) -> tuple[float, float]:
    long_num = short_num = w_sum = 0.0
    for name, base_w in priorities.items():
        eng = engines.get(name)
        if eng is None or base_w <= 0:
            continue
        eff = base_w * max(eng.coverage_quality, 0.05)
        long_num += eng.long * eff
        short_num += eng.short * eff
        w_sum += eff
    if w_sum <= 0:
        return 0.5, 0.5
    return clamp01(long_num / w_sum), clamp01(short_num / w_sum)


def blend_horizons(
    engines: dict[str, EngineOutput],
    cfg: VerdictV2Config,
) -> dict[str, HorizonForecast]:
    specs: list[tuple[HorizonKey, dict[str, float]]] = [
        ("A", cfg.priorities_a),
        ("B", cfg.priorities_b),
        ("C", cfg.priorities_c),
    ]
    out: dict[str, HorizonForecast] = {}
    for key, pri in specs:
        lg, sh = _blend(engines, pri)
        dom = dominant_side(lg, sh)
        conv = conviction(lg, sh)
        range_p = clamp01(1.0 - conv) if dom == "neutral" else clamp01(max(0.0, 0.5 - conv))
        out[key] = HorizonForecast(
            key=key,
            long=round(lg, 3),
            short=round(sh, 3),
            dominant=dom,  # type: ignore[arg-type]
            conviction=round(conv, 3),
            range_probability=round(range_p, 3),
        )
    return out


def build_conflict_matrix(engines: dict[str, EngineOutput]) -> dict[str, float]:
    """R7: direction_diff × min(conviction_a, conviction_b)."""
    names = list(engines.keys())
    matrix: dict[str, float] = {}
    for i, a in enumerate(names):
        ea = engines[a]
        for b in names[i + 1 :]:
            eb = engines[b]
            dir_a = ea.long - ea.short
            dir_b = eb.long - eb.short
            direction_diff = abs(dir_a - dir_b) / 2.0
            c_min = min(ea.conviction, eb.conviction)
            matrix[f"{a}×{b}"] = round(clamp01(direction_diff * c_min), 3)
    return matrix
