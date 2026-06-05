"""Research harvest mode — profile overrides and recorder."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from bot.diagnostics.research_harvest import ResearchHarvestRecorder
from bot.domain.config import BotSettings, ResearchHarvestConfig
from bot.domain.research_harvest import (
    DEFAULT_RESEARCH_HARVEST_SYMBOLS,
    _resolved_symbols,
    activate_research_harvest,
    apply_research_harvest_profile,
)
from bot.domain.schemas import PipelineResult


def _minimal_settings() -> BotSettings:
    return BotSettings.model_validate(
        {
            "tg_token": "x",
            "target_chat_id": "1",
            "notifiers": {"provider": "telegram"},
        }
    )


def test_resolved_symbols_includes_pins_and_defaults() -> None:
    rh = ResearchHarvestConfig(enabled=True, symbols=())
    symbols = _resolved_symbols(rh)
    assert "BTCUSDT" in symbols
    assert "PAXGUSDT" in symbols
    assert len(symbols) >= len(DEFAULT_RESEARCH_HARVEST_SYMBOLS)


def test_activate_harvest_disables_telegram_and_pins_shortlist() -> None:
    base = _minimal_settings()
    tuned = activate_research_harvest(base, symbols=("BTCUSDT", "ETHUSDT"))
    assert tuned.research_harvest.enabled is True
    assert tuned.notifiers.provider == "none"
    assert tuned.universe.shortlist_limit == len(_resolved_symbols(tuned.research_harvest))
    assert tuned.runtime.route_all_enabled_strategies is True
    assert tuned.universe.radar.enabled is False


def test_apply_profile_noop_when_disabled() -> None:
    base = _minimal_settings()
    assert apply_research_harvest_profile(base) is base


def test_recorder_writes_manifest_and_cycles(tmp_path: Path) -> None:
    recorder = ResearchHarvestRecorder(
        root_dir=tmp_path,
        run_id="test_run",
        symbols=("BTCUSDT",),
        config_path=Path("config.toml"),
    )

    recorder.record_cycle(
        symbol="BTCUSDT",
        interval="15m",
        event_ts=datetime.now(UTC),
        result=PipelineResult(
            symbol="BTCUSDT",
            trigger="kline_close",
            event_ts=datetime.now(UTC),
            raw_setups=3,
        ),
        candidates=[],
        rejected=[],
        prepared_snapshot_row={"symbol": "BTCUSDT", "spread_bps": 2.0},
    )
    out = recorder.finalize()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cycle_records"] == 1
    assert (out / "cycles.jsonl").exists()
    assert (out / "symbols" / "BTCUSDT" / "cycles.jsonl").exists()
