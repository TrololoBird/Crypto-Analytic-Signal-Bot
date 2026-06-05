#!/usr/bin/env python3
"""Generate docs/PY_FILE_AUDIT_5X.md — five improvement bullets per .py file."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "PY_FILE_AUDIT_5X.md"

# Package-level default bullets (rotated + file-specific suffix)
PACKAGE_RULES: dict[str, list[str]] = {
    "bot/market/": [
        "Согласовать REST weight budget с shortlist/radar tier refresh.",
        "Логировать fallback_reason и proxy lane в telemetry (market/*.jsonl).",
        "Не дублировать кэш с ws_cache — единый источник ticker/kline.",
        "Покрыть live pytest сценарий при PYTEST_LIVE=1 (public REST only).",
        "Документировать контракт полей в ARCHITECTURE_CANONICAL / RADAR_FUNNEL.",
    ],
    "bot/runtime/": [
        "Уменьшить связность с bot.py — делегировать в runner/updater.",
        "Таймауты asyncio.wait_for на внешние вызовы (Binance/DB).",
        "Единый run_id в telemetry append_jsonl для live_watch bridge.",
        "Не глотать исключения без LOG + telemetry error event.",
        "Wave-тест при изменении hot path (F9/F10 agent module).",
    ],
    "bot/strategies/": [
        "Пороги из strategy_catalog + post-live calibration_pipeline.",
        "build_smc_trade_plan / swing_series где SMC (OB/FVG/sweep).",
        "Wilder ATR/RSI через prepare_frame — без pandas shift(-N).",
        "Lane routing: не регистрировать на bookTicker без order-flow need.",
        "Тест: synthetic frame + live_check_strategies для zero-hit triage.",
    ],
    "bot/delivery/": [
        "Путь только contract → hard_confluence_gate → deliver (никогда bypass).",
        "Стадии filter_stages — явный reason в selected/rejected JSONL.",
        "WATCH vs ACTION tiers согласованы с delivery_policy.toml.",
        "HTML/Telegram формат — validate_signal_contract перед send.",
        "delivery-guardian audit после любого изменения filters/confluence.",
    ],
    "bot/features/": [
        "Только Polars expressions; ddof=1 для BB; Wilder для RSI/ATR.",
        "Без shift(-N) на live path; document lookback в INDICATORS.md.",
        "prepare_symbol height limits согласованы с primary_timeframe config.",
        "Кэш feature store не смешивать между run_id (clean_session_data).",
        "live_check_indicators при изменении prepare_frame.py.",
    ],
    "bot/persistence/": [
        "Миграции forward-only; journal normalize_tracking_event.",
        "JSON-serializable payloads (no raw pl.DataFrame in SQLite JSON).",
        "Split bloated memory.py queries → persistence/queries/ (F12).",
        "Retention policy для outcomes/diary согласован с config.",
        "db migrate + wave test после schema change.",
    ],
    "bot/regime/": [
        "regime_frame_4h JSON-safe в benchmark_context (не pl в DB).",
        "global_market_regime + btc_phase согласованы с family_gates.",
        "Кэш TTL regime — не блокировать event loop тяжёлым HMM.",
        "Тест composite + minimal frame columns.",
        "Документ bear carve-out в SIGNAL_EVALUATION.md.",
    ],
    "bot/engine/": [
        "calculate_all timeout и lane_skips в cycle telemetry.",
        "Registry wiring = strategy_catalog keys only.",
        "Не дублировать детекторы из setups/ в engine.",
        "perf_rejected причины в strategy_errors JSONL.",
        "test_engine_routing при новых families.",
    ],
    "bot/dashboard/": [
        "API /api/* без private Binance; read-only operator actions.",
        "Funnel KPI = delivered sent/logged only (F10 T).",
        "Radar tier endpoint (P2) — hot/warm/deep counts.",
        "ws_broadcast не блокировать event loop.",
        "Smoke: dashboard /api/health при BOT_DISABLE_HTTP_SERVERS=0.",
    ],
    "bot/domain/": [
        "Strict pydantic/TOML models; validate_config на example.",
        "catalog_guards: param keys ⊆ strategy_catalog.",
        "Не добавлять trading/order placement поля.",
        "RU labels для operator Telegram.",
        "test_config_audit при новых полях universe.radar.",
    ],
    "bot/diagnostics/": [
        "shortlist_not_routed семантика в live_audit.",
        "Quality monitor JSON не пересекать run_id.",
        "config_audit drift vs config.toml.example.",
        "Связать с calibration_pipeline run-id slice.",
        "project_health_audit gate в CI/Makefile.",
    ],
    "bot/setups/": [
        "spec_runtime tier согласован со STRATEGY_CATALOG.",
        "SMC utils общие с strategies (no duplicate tree).",
        "Unit test на edge cases (empty frame, NaN).",
        "Не импортировать runtime.bot (layer violation).",
        "Rewrite если >500 LOC без split plan.",
    ],
    "scripts/": [
        "bootstrap_repo_path + --config default config.toml.",
        "Идемпотентность; не требовать ручных шагов оператора.",
        "Докstring с примером CLI в SOLO_OPERATOR_PLAYBOOK.",
        "launch_detached/start_new_session для long live runs.",
        "compileall/gate не ломать при добавлении скрипта.",
    ],
    "tests/": [
        "Wave agent test name stable для refactor gate.",
        "Live tests только tests/live/ + PYTEST_LIVE=1.",
        "Не mock Binance private API.",
        "Fixtures из minimal Polars frames.",
        "Добавлять тест при bugfix (regression).",
    ],
}

FILE_OVERRIDES: dict[str, list[str]] = {
    "bot/runtime/market_context_updater.py": [
        "P0: regime_frame_4h сериализовать в JSON (list columns), не pl.DataFrame в DB.",
        "После analyze() очищать benchmark_context от non-JSON типов.",
        "Ограничить fetch_ticker_24h частоту — weight budget.",
        "Тест regression: update_market_context не падает на serialize.",
        "Разбить _build_market_state_text в отдельный модуль (LOC).",
    ],
    "bot/runtime/shortlist_service.py": [
        "P0: писать radar + radar_tier_cycle в shortlist_build.jsonl (сейчас только runtime).",
        "Единый _prepare_tickers_with_radar для всех refresh sources.",
        "Логировать promotion_slots_used vs reserve.",
        "Тест merge frozen UniverseSymbol (radar funnel).",
        "De-bloat: вынести REST/WS refresh в подмодули.",
    ],
    "bot/market/radar_state.py": [
        "P2: hot tier — dynamic @kline_1m subscribe (planner).",
        "Ring buffer size config-driven; telemetry tier counts.",
        "Thread-safe ingest при WS burst !ticker@arr.",
        "Demotion idle vs promotion cooldown — unit tests.",
        "Dashboard API consumer (tier snapshot).",
    ],
    "bot/market/universe_screener.py": [
        "P2: REST 1h klines для real RSI на warm pool (сейчас proxy).",
        "emit_watch_candidates → watch_escalation (config gate).",
        "Согласовать funding_extreme_pct с config fraction.",
        "Флаги screener в shortlist_reasons для calibration.",
        "Property test на prescore_boost bounds.",
    ],
    "bot/market/promotion_engine.py": [
        "dataclasses.replace для frozen UniverseSymbol (done — держать тест).",
        "radar_tier в enriched ticker rows для matrix.",
        "merge_shortlist не дублировать pinned.",
        "Telemetry radar_tiers snapshot each cycle.",
        "Document merge rules in RADAR_FUNNEL.md.",
    ],
    "bot/market/universe.py": [
        "P1: wash/spread gates telemetry при reject.",
        "radar_prescore_boost в _prescore_row — калибровка после 6h.",
        "REST weight budget — не starve deep refresh.",
        "outcome_derank integration tested.",
        "Split build_shortlist phases (F12).",
    ],
    "bot/market/ws.py": [
        "F12: продолжить split ws_connection/ws_cache.",
        "Subscription budget vs shortlist size=50.",
        "set_radar_store lifecycle при reconnect.",
        "!ticker@arr backpressure metrics.",
        "live pytest WS catalog wiring.",
    ],
    "bot/runtime/bot.py": [
        "F12: thin orchestration — логика в runners/analyzer.",
        "update_memory_market_context error rate telemetry.",
        "intra_candle vs kline handler — no double analyze.",
        "cycle timeout из config strict.",
        "Не расти >1015 LOC без split PR.",
    ],
    "bot/persistence/repository/memory.py": [
        "F12 P0 split: queries → persistence/queries/*.",
        "benchmark_context JSON encoder для pl/numpy.",
        "update_market_context atomic.",
        "Diary/journal symbol normalization (v6).",
        "Tracking stats timeout 1s — уже есть, документировать.",
    ],
    "bot/runtime/analyzer/pipeline.py": [
        "F12: extract cycle dispatch / conflict merge.",
        "family_gates единая точка MTF reject reasons.",
        "Не блокировать delivery semaphore.",
        "Telemetry per-stage latency.",
        "test_wave_f10_agent_l coverage.",
    ],
    "scripts/live_supervised_session.py": [
        "P1: default launch via launch_detached.py (survive Cursor shell exit).",
        "session_meta.json автозапись при session_start.",
        "rollup + calibration_pipeline hook в session_finished.",
        "takeover exclude supervisor own pid tree.",
        "minute_snapshot errors → non-zero exit optional.",
    ],
    "scripts/launch_detached.py": [
        "P1: wrapper для live_supervised 6h (document in playbook).",
        "PID file + log rotation.",
        "Проверка child alive после 60s.",
        "SIGTERM graceful cascade to main.py stop.",
        "Makefile target live-detached-6h.",
    ],
    "scripts/agent_bot_supervisor.py": [
        "Align MAX_RUNTIME with config supervised hours.",
        "Snapshot radar tier in agent_live_monitor.",
        "Не дублировать live_supervised если оба запущены.",
        "calibration_note post 6h.",
        "start_new_session как live_supervised.",
    ],
    "main.py": [
        "Только entry → bot.cli.run.",
        "PYTHONPATH / venv 3.14 documented.",
        "Не добавлять бизнес-логику.",
        "—",
        "—",
    ],
}


def _rules_for(rel: str) -> list[str]:
    if rel in FILE_OVERRIDES:
        return FILE_OVERRIDES[rel]
    for prefix, bullets in PACKAGE_RULES.items():
        if rel.startswith(prefix):
            return bullets
    return [
        "Проверить import layer (domain → market/features → runtime).",
        "Нет auto-trading / private Binance API.",
        "compileall + refactor gate после изменений.",
        "graphify update после правок.",
        "Удалить мёртвый код или пометить deprecated.",
    ]


def _loc(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def generate(*, include_tests: bool, include_scripts: bool) -> str:
    roots = [ROOT / "bot"]
    if include_scripts:
        roots.append(ROOT / "scripts")
    if include_tests:
        roots.append(ROOT / "tests")
    if (ROOT / "main.py").is_file():
        roots.append(ROOT / "main.py")

    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))

    lines: list[str] = [
        "# Аудит: 5 улучшений на каждый `.py` файл",
        "",
        "> Сгенерировано `scripts/generate_py_audit_5x.py`. Приоритеты P0–P2 — в "
        "[PY_IMPLEMENTATION_PLAN.md](PY_IMPLEMENTATION_PLAN.md).",
        "",
        f"**Файлов:** {len(files)}",
        "",
    ]
    by_pkg: dict[str, list[Path]] = {}
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        parts = rel.split("/")
        if len(parts) >= 2:
            pkg = "/".join(parts[:2])
        else:
            pkg = parts[0]
        by_pkg.setdefault(pkg, []).append(f)

    for pkg in sorted(by_pkg):
        lines.append(f"## `{pkg}/`")
        lines.append("")
        for path in by_pkg[pkg]:
            rel = path.relative_to(ROOT).as_posix()
            loc = _loc(path)
            bullets = _rules_for(rel)
            lines.append(f"### `{rel}` ({loc} LOC)")
            for i, b in enumerate(bullets, 1):
                lines.append(f"{i}. {b}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--no-scripts", action="store_true")
    parser.add_argument("-o", "--output", type=Path, default=OUT)
    args = parser.parse_args()
    text = generate(
        include_tests=not args.no_tests,
        include_scripts=not args.no_scripts,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
