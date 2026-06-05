from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from .gmm_var import HAS_SKLEARN, HAS_STATSMODELS, CentroidRegimeDetector
from .hmm_regime import HAS_HMMLEARN, RuleBasedRegimeDetector

ML_COMPONENTS_AVAILABLE = HAS_HMMLEARN and HAS_SKLEARN and HAS_STATSMODELS


def benchmark_funding_median(funding_rates: dict[str, float] | None) -> float:
    """Median BTC+ETH funding for composite/centroid features (N5-lite)."""
    if not funding_rates:
        return 0.0
    samples: list[float] = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        raw = funding_rates.get(symbol)
        if raw is None:
            continue
        try:
            samples.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not samples:
        return 0.0
    samples.sort()
    mid = len(samples) // 2
    if len(samples) % 2:
        return samples[mid]
    return (samples[mid - 1] + samples[mid]) / 2.0


def build_minimal_regime_frame_4h(
    closes: list[float],
    *,
    window: int = 20,
) -> pl.DataFrame | None:
    """Build rule/HMM features from benchmark 4h closes (N4-lite)."""
    clean = [float(value) for value in closes if float(value) > 0.0]
    if len(clean) < 3:
        return None
    frame = pl.DataFrame({"close": clean})
    log_returns = frame["close"].log() - frame["close"].shift(1).log()
    roll_window = max(2, min(window, len(clean) - 1))
    return frame.with_columns(
        log_returns.fill_null(0.0).alias("log_returns"),
        log_returns.abs()
        .rolling_std(window_size=roll_window)
        .fill_null(0.0)
        .alias("realized_vol"),
        (log_returns.abs() * 100.0).fill_null(0.0).alias("atr_pct"),
    ).select("log_returns", "realized_vol", "atr_pct")


@dataclass(frozen=True)
class RegimeResult:
    regime: str
    strength: float
    confidence: float


class CompositeRegimeAnalyzer:
    def __init__(self) -> None:
        self.rule_based = RuleBasedRegimeDetector()
        self.centroid = CentroidRegimeDetector()

    def analyze(
        self,
        _ticker_data: list[dict[str, Any]],
        funding_rates: dict[str, float] | None,
        benchmark_context: dict[str, dict[str, Any]] | None,
    ) -> RegimeResult:
        benchmark_context = benchmark_context or {}
        btc = benchmark_context.get("BTCUSDT", {})
        returns = float(btc.get("basis_pct") or 0.0)
        vol = abs(float(btc.get("premium_slope_5m") or 0.0))
        funding = benchmark_funding_median(funding_rates)

        if not ML_COMPONENTS_AVAILABLE:
            return self._rule_based_fallback(
                benchmark_context=benchmark_context,
                returns=returns,
                vol=vol,
                funding=funding,
            )

        centroid_regime, centroid_conf = self.centroid.current_regime(
            {"returns": returns, "vol": vol, "funding_rate": funding}
        )
        rule_based_pred = self.rule_based.predict(
            self._build_rule_based_frame(
                benchmark_context=benchmark_context, returns=returns, vol=vol
            )
        )

        centroid_vote = self._map_centroid(centroid_regime)
        rule_based_vote = self._map_rule_based(rule_based_pred.regime)
        legacy_vote = self._legacy_vote(returns, vol, funding)

        vote_weights = {"centroid": 0.4, "rule_based": 0.4, "legacy": 0.2}
        weighted_scores: dict[str, float] = {
            "bull": 0.0,
            "bear": 0.0,
            "ranging": 0.0,
            "volatile": 0.0,
        }
        weighted_scores[centroid_vote] += vote_weights["centroid"]
        weighted_scores[rule_based_vote] += vote_weights["rule_based"]
        weighted_scores[legacy_vote] += vote_weights["legacy"]

        regime = max(weighted_scores.items(), key=lambda item: item[1])[0]
        strength = max(0.45, min(0.9, weighted_scores[regime]))
        confidence = min(0.95, (centroid_conf * 0.5) + (rule_based_pred.confidence * 0.5))
        return RegimeResult(regime=regime, strength=strength, confidence=confidence)

    @property
    def gmm(self) -> CentroidRegimeDetector:
        # backward-compat: remove in v9.0
        """Backward-compatible alias for older tests/callers."""
        return self.centroid

    @property
    def hmm(self) -> RuleBasedRegimeDetector:
        # backward-compat: remove in v9.0
        """Backward-compatible alias for older tests/callers."""
        return self.rule_based

    def _rule_based_fallback(
        self,
        *,
        benchmark_context: dict[str, dict[str, Any]],
        returns: float,
        vol: float,
        funding: float,
    ) -> RegimeResult:
        prediction = self.rule_based.predict(
            self._build_rule_based_frame(
                benchmark_context=benchmark_context,
                returns=returns,
                vol=vol,
            )
        )
        rule_vote = self._map_rule_based(prediction.regime)
        legacy_vote = self._legacy_vote(returns, vol, funding)
        regime = rule_vote if rule_vote != "ranging" else legacy_vote
        strength = max(0.45, min(0.8, prediction.confidence))
        confidence = max(0.0, min(0.75, prediction.confidence))
        return RegimeResult(regime=regime, strength=strength, confidence=confidence)

    @staticmethod
    def _build_rule_based_frame(
        *,
        benchmark_context: dict[str, dict[str, Any]],
        returns: float,
        vol: float,
    ) -> pl.DataFrame:
        btc = benchmark_context.get("BTCUSDT", {})
        history = btc.get("regime_frame_4h")
        if isinstance(history, dict) and history:
            history = pl.DataFrame(history)
        if isinstance(history, pl.DataFrame) and not history.is_empty():
            required = {"log_returns", "realized_vol", "atr_pct"}
            if required.issubset(set(history.columns)):
                return history.select(sorted(required))

        return pl.DataFrame(
            {
                "log_returns": [returns],
                "realized_vol": [vol],
                "atr_pct": [abs(vol)],
            }
        )

    @staticmethod
    def _map_centroid(regime: str) -> str:
        if regime == "contagion":
            return "volatile"
        if regime == "calm_up":
            return "bull"
        if regime == "calm_down":
            return "bear"
        return "ranging"

    @staticmethod
    def _map_rule_based(regime: str) -> str:
        if regime == "high_vol_choppy":
            return "volatile"
        if regime == "low_vol_uptrend":
            return "bull"
        if regime == "low_vol_downtrend":
            return "bear"
        return "ranging"

    @staticmethod
    def _legacy_vote(returns: float, vol: float, funding: float) -> str:
        if vol >= 0.02:
            return "volatile"
        if returns > 0 and funding >= -0.0005:
            return "bull"
        if returns < 0 and funding <= 0.0005:
            return "bear"
        return "ranging"
