from __future__ import annotations

from dataclasses import dataclass

BASELINE_MODEL_KINDS = frozenset({"centroid_baseline", "linear_baseline"})


@dataclass(frozen=True, slots=True)
class LiveModelGuardrailDecision:
    model_kind: str
    stage: str
    is_live: bool
    disable_reason: str | None

    @property
    def should_disable(self) -> bool:
        return self.disable_reason is not None


def evaluate_live_model_guardrail(
    *,
    is_live: bool,
    model_kind: str,
    stage: str,
) -> LiveModelGuardrailDecision:
    normalized_model_kind = str(model_kind or "unknown").strip().lower() or "unknown"
    normalized_stage = str(stage or "unknown").strip().lower() or "unknown"
    disable_reason: str | None = None
    if is_live and normalized_model_kind in BASELINE_MODEL_KINDS:
        disable_reason = "live_baseline_blocked"

    return LiveModelGuardrailDecision(
        model_kind=normalized_model_kind,
        stage=normalized_stage,
        is_live=bool(is_live),
        disable_reason=disable_reason,
    )
