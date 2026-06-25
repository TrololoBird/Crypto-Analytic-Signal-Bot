"""ExpansionProbabilityModel — P(up), P(down), P(none).

Replaces binary PrePump/PreDump detectors. The market is not binary: a coin can sit near
a distribution pivot with both sides elevated (e.g. P(up)=0.61, P(down)=0.55). The model
accumulates direction-weighted evidence from the blocks, blends in delta momentum, and
softmaxes against a "no expansion" baseline.
"""
from __future__ import annotations

import math

from hunt_core.expansion._util import clamp01
from hunt_core.expansion.config import ExpansionConfig
from hunt_core.expansion.types import (
    BlockDeltas,
    BlockResult,
    ExpansionProbabilities,
)

_LOGIT_SCALE = 3.6


class ExpansionProbabilityModel:
    def __init__(self, cfg: ExpansionConfig) -> None:
        self._cfg = cfg

    def _evidence(self, blocks: dict[str, BlockResult], weights: dict[str, float], side: str) -> float:
        total = 0.0
        for name, res in blocks.items():
            if not res.active or res.direction != side:
                continue
            total += weights.get(name, 0.0) * res.score
        return clamp01(total)

    def predict(
        self,
        blocks: dict[str, BlockResult],
        deltas: BlockDeltas,
        stage: int,
    ) -> ExpansionProbabilities:
        up_raw = self._evidence(blocks, self._cfg.up_weights, "up")
        down_raw = self._evidence(blocks, self._cfg.down_weights, "down")

        # Delta momentum tilts the already-dominant side (changes precede pumps).
        momentum = clamp01(getattr(deltas, "momentum", 0.5))
        if up_raw >= down_raw:
            up_raw = clamp01(up_raw * (0.85 + 0.30 * momentum))
        else:
            down_raw = clamp01(down_raw * (0.85 + 0.30 * momentum))

        strong = max(up_raw, down_raw)
        none_raw = clamp01(1.0 - strong) * 0.9

        logits = (
            _LOGIT_SCALE * up_raw,
            _LOGIT_SCALE * down_raw,
            _LOGIT_SCALE * none_raw,
        )
        m = max(logits)
        exps = [math.exp(x - m) for x in logits]
        s = sum(exps) or 1.0
        p_up, p_down, p_none = (e / s for e in exps)
        return ExpansionProbabilities(
            p_up=round(p_up, 4),
            p_down=round(p_down, 4),
            p_none=round(p_none, 4),
        )


__all__ = ["ExpansionProbabilityModel"]
