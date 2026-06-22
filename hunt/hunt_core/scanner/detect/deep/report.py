"""Deep-analysis orchestrator — assemble panel + scenarios + verdict + Telegram text.

``build_deep_report`` works on a prepared :class:`FeatureWindow`, so the live ``/signal``
path (rich frame) and the offline lake path share one code path.
``build_deep_report_from_lake`` is the offline convenience used by the dev smoke and
pinned-symbol review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from hunt_core.scanner.detect.deep.forecast import Scenario, forecast_scenarios
from hunt_core.scanner.detect.deep.format import format_deep_telegram
from hunt_core.scanner.detect.deep.panel import DeepPanel, build_panel
from hunt_core.scanner.detect.deep.verdict import Verdict, build_verdict
from hunt_core.scanner.detect.windows import DEFAULT_LOOKBACK, FeatureWindow, build_window


@dataclass(frozen=True)
class DeepReport:
    panel: DeepPanel
    scenarios: list[Scenario] = field(default_factory=list)
    verdict: Verdict | None = None
    text: str = ""

    @property
    def symbol(self) -> str:
        return self.panel.symbol

    def summary(self) -> dict[str, object]:
        return {
            "symbol": self.panel.symbol,
            "side": self.panel.side,
            "phase": self.panel.phase.phase,
            "confidence": round(self.panel.confidence, 4),
            "stance": self.verdict.stance if self.verdict else "neutral",
            "actionable": self.verdict.actionable if self.verdict else False,
        }


def build_deep_report(window: FeatureWindow) -> DeepReport:
    """Full deep report for a prepared trailing window (any symbol, any phase)."""
    panel = build_panel(window)
    scenarios = forecast_scenarios(window, panel)
    verdict = build_verdict(panel)
    text = format_deep_telegram(panel, scenarios, verdict)
    return DeepReport(panel=panel, scenarios=scenarios, verdict=verdict, text=text)


def build_deep_report_from_lake(
    symbol: str,
    *,
    tf: str = "15m",
    lookback: int = DEFAULT_LOOKBACK,
) -> DeepReport | None:
    """Build a deep report from the parquet lake (offline / pinned review)."""
    from hunt_core.paths import LAKE_PARQUET

    path = LAKE_PARQUET / symbol.upper() / f"{tf}.parquet"
    if not path.exists():
        return None
    df = pl.read_parquet(path)
    if df.height == 0:
        return None
    if "ts" in df.columns:
        df = df.sort("ts")
    window = build_window(df, symbol=symbol, tf=tf, lookback=lookback)
    return build_deep_report(window)


__all__ = ["DeepReport", "build_deep_report", "build_deep_report_from_lake"]
