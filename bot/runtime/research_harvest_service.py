"""Runtime hook: record rich cycle snapshots during research harvest mode."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.diagnostics.research_harvest import ResearchHarvestRecorder, prepared_snapshot
from bot.domain.research_harvest import _resolved_symbols

if TYPE_CHECKING:
    from bot.domain.schemas import PipelineResult

LOG = logging.getLogger("bot.runtime.research_harvest")


class ResearchHarvestService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        settings = bot.settings
        rh = settings.research_harvest
        self._symbols = _resolved_symbols(rh)
        root = Path(settings.data_dir).parent / rh.output_subdir
        run_id = str(getattr(bot.telemetry, "run_id", "") or "").strip()
        if not run_id:
            run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.recorder = ResearchHarvestRecorder(
            root_dir=root,
            run_id=run_id,
            symbols=self._symbols,
            config_path=Path(settings.config_path),
        )
        LOG.info(
            "research_harvest_active | run_id=%s symbols=%d dir=%s",
            run_id,
            len(self._symbols),
            self.recorder.session_dir,
        )

    def record_cycle(
        self,
        *,
        symbol: str,
        interval: str,
        event_ts: datetime,
        result: PipelineResult,
        candidates: list[Any],
        rejected: list[dict[str, Any]],
    ) -> None:
        rh = self._bot.settings.research_harvest
        if not rh.enabled or not rh.snapshot_on_cycle:
            return
        telemetry_mgr = self._bot._get_telemetry_manager()
        indicator_snapshot = None
        if rh.include_indicator_snapshot and result.prepared is not None:
            indicator_snapshot = telemetry_mgr._indicator_snapshot(result.prepared)
        ws_enrichments = None
        if rh.include_ws_enrichments:
            try:
                ws_enrichments = self._bot._ws_cache_enrichments(symbol)
            except (AttributeError, KeyError, TypeError, ValueError):
                ws_enrichments = {}
        snap = prepared_snapshot(
            result.prepared,
            indicator_snapshot=indicator_snapshot,
            ws_enrichments=ws_enrichments,
            max_bar_tail=rh.max_bar_tail,
        )
        if rh.include_reject_log and result.prepared is not None and result.prepared.reject_log:
            if snap is None:
                snap = {"symbol": symbol}
            snap["reject_log"] = list(result.prepared.reject_log[:50])
        self.recorder.record_cycle(
            symbol=symbol,
            interval=interval,
            event_ts=event_ts,
            result=result,
            candidates=candidates,
            rejected=rejected,
            prepared_snapshot_row=snap,
        )

    def finalize(self) -> Path:
        telemetry_dir = getattr(self._bot.telemetry, "run_dir", None)
        extra: dict[str, Any] = {}
        if telemetry_dir is not None:
            extra["telemetry_run_dir"] = str(telemetry_dir)
        path = self.recorder.finalize(extra=extra)
        LOG.info("research_harvest_finalized | dir=%s", path)
        return path
