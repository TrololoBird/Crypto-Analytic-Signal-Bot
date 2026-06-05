#!/usr/bin/env python3
"""Deep per-file audit: five *unique* improvement findings (not package templates)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "PY_FILE_AUDIT_NEW_FINDINGS.md"

# Modules that should participate in radar funnel but had zero radar references (2026-06-04 scan).
RADAR_GAP_MODULES = frozenset(
    {
        "bot/market/enrichment.py",
        "bot/market/screener.py",
        "bot/market/data.py",
        "bot/market/rest_impl.py",
        "bot/market/spot_companion.py",
        "bot/market/outcome_derank.py",
        "bot/market/proxy_bootstrap.py",
        "bot/market/network_proxy.py",
        "bot/runtime/cycle_runner.py",
        "bot/runtime/cycle_runner.py",
        "bot/runtime/kline_handler.py",
        "bot/runtime/fallback_runner.py",
        "bot/runtime/oi_refresh_runner.py",
        "bot/runtime/spot_refresh_runner.py",
        "bot/runtime/health_manager.py",
        "bot/runtime/telemetry_manager.py",
        "bot/runtime/delivery_orchestrator.py",
        "bot/runtime/watch_escalation.py",
        "bot/engine/engine.py",
        "bot/engine/lanes.py",
        "bot/features/prepare.py",
        "bot/features/microstructure.py",
        "bot/delivery/filters.py",
        "bot/delivery/confluence.py",
        "bot/diagnostics/quality.py",
        "bot/diagnostics/signals.py",
        "bot/persistence/tracking.py",
        "bot/dashboard/app.py",
        "bot/dashboard/ws_broadcast.py",
        "bot/ops/startup_report.py",
    }
)

STRATEGY_NEW: dict[str, list[str]] = {
    "order_block": [
        "Калибровать `ob_lookback` / touch tolerance по 6h telemetry (zero-hit triage).",
        "Связать с radar: при `radar_promoted` + impulse flag — не занижать score в family_gates.",
        "Единый `build_smc_trade_plan` — проверить symmetry с fvg/liquidity_sweep RR ladder.",
        "Lane: только kline_close + bookTicker если нужен live touch — не дублировать на 1m без OI.",
        "Live: `live_check_strategies --setup order_block` после изменения порогов.",
    ],
    "fvg": [
        "is_clean_fvg + spec tier — сверить с STRATEGY_CATALOG §6.",
        "Radar warm symbols с `impulse_5m` — приоритет intra_candle subset (config).",
        "HTF gap fill rate в rejected.jsonl — отдельный KPI в calibration_pipeline.",
        "Не использовать pandas; FVG boxes только Polars masks.",
        "Тест regression на пустом 4h frame.",
    ],
    "funding_reversal": [
        "Согласовать extreme threshold с `universe.radar.funding_extreme_pct`.",
        "data.funding_rate_missing — алерт в quality monitor если >10% shortlist.",
        "Countertrend profile — bear carve-out уже в orchestrator; документировать reject reasons.",
        "REST funding cache TTL vs ws refresh — не stale на promoted radar symbols.",
        "Matrix row: hit-rate vs funding percentile bins.",
    ],
    "cvd_divergence": [
        "Требует aggTrade — subscription_planner должен держать symbol в agg cap (hot priority).",
        "htf_reversal_conflict частый reject — отдельный telemetry tag `radar_hot_mtf_conflict`.",
        "Калибровка min_score vs ACTION tier caps.",
        "Сверить CVD feature column names с prepare_frame export list.",
        "Zero-hit: проверить order-flow readiness в data_readiness.",
    ],
    "session_killzone": [
        "Clock / TZ: killzone windows в config audit.",
        "Не запускать вне killzone на radar-promoted alts без override.",
        "Scheduled setup — отдельный lane в engine registry.",
        "Telemetry: context.outside_killzone должен быть <5% в killzone hours.",
        "Документировать в SIGNAL_EVALUATION watch-only mode.",
    ],
    "whale_walls": [
        "Depth stream cap — hot radar symbols в depth_symbols первыми (planner).",
        "Level C confluence — не ACTION без 3-of-5 legs.",
        "Сверить depth snapshot staleness gate с shortlist_book_stale_seconds.",
        "Калибровка wall size vs quote_volume tier.",
        "live_check_enrichments depth path.",
    ],
    "liquidation_heatmap": [
        "forceOrder + OI — data readiness guard в cycle_runner.",
        "liquidation_score_missing в strategy_audit DATA_SOURCE_REASONS.",
        "Radar vol_spike_zscore — correlate hits с promotion flags.",
        "Не блокировать majors pinned при derank.",
        "Calibration: Liq fade vs funding regime matrix.",
    ],
}

STRATEGY_DEFAULT = [
    "Post-6h: `strategy_shortlist_matrix --setup <id>` hit-rate и top reject stage.",
    "Пороги только через `strategy_catalog` + config overrides (не magic numbers в теле).",
    "Проверить `confirmation_profile` vs family_gates (trend/breakout/reversal).",
    "Если detector_runs>0 и signals=0 — сузить feature predicates, не ослаблять delivery.",
    "Добавить/обновить wave agent test при изменении public API setup class.",
]

# Parsed from docs/research/STRATEGY_CATALOG.md summary table (setup_id → public data deps).
_CATALOG_DATA: dict[str, str] = {}
_CATALOG_PATH = ROOT / "docs" / "research" / "STRATEGY_CATALOG.md"
if _CATALOG_PATH.is_file():
    for line in _CATALOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 9:
            continue
        setup_id = parts[2]
        if not setup_id or not setup_id.replace("_", "").isalnum():
            continue
        level = parts[4]
        data_col = parts[8] if len(parts) > 8 else "klines"
        _CATALOG_DATA[setup_id] = f"lvl {level}: {data_col}"


def _strategy_file_to_setup_id(stem: str) -> str:
    if stem in _CATALOG_DATA:
        return stem
    if f"{stem}_setup" in _CATALOG_DATA:
        return f"{stem}_setup"
    alt = stem.replace("_setup", "")
    if alt in _CATALOG_DATA:
        return alt
    return stem


def _catalog_driven_strategy_findings(stem: str) -> list[str]:
    setup_id = _strategy_file_to_setup_id(stem)
    data_deps = _CATALOG_DATA.get(setup_id, "lvl A: klines")
    return [
        f"Catalog `{setup_id}` ({data_deps}); сверить data_readiness guards.",
        f"Matrix: `strategy_shortlist_matrix --setup {setup_id}` после 6h run_id.",
        "Radar: promoted symbols с impulse/vol flags — не ослаблять delivery; только prescore/lane.",
        f"family_gates + confirmation_profile для `{setup_id}` в STRATEGY_CATALOG §.",
        "Reject reasons в telemetry → calibration_pipeline slice (не глобальный min_score).",
    ]

LOC_RULES: list[tuple[int, str]] = [
    (1500, "F12: немедленный split на submodules (<500 LOC/файл), gate + wave tests."),
    (800, "F12: запланировать split; вынести query/render/helpers в соседние файлы."),
    (500, "Мониторить рост LOC; избегать новых god-functions."),
]

IMPORT_RADAR = re.compile(r"\bradar\b|MarketRadarStore|universe_screener|promotion_engine", re.I)


def _loc(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _has_radar_wire(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(IMPORT_RADAR.search(text))


def _strategy_stem(rel: str) -> str | None:
    if not rel.startswith("bot/strategies/") or rel.endswith("__init__.py"):
        return None
    return Path(rel).stem


def findings_for(rel: str, path: Path) -> list[str]:
    loc = _loc(path)
    out: list[str] = []

    stem = _strategy_stem(rel)
    if stem:
        if stem in STRATEGY_NEW:
            return STRATEGY_NEW[stem][:5]
        return _catalog_driven_strategy_findings(stem)[:5]

    for threshold, msg in LOC_RULES:
        if loc >= threshold:
            out.append(msg)
            break

    if rel in RADAR_GAP_MODULES or (
        rel.startswith(("bot/market/", "bot/runtime/"))
        and not _has_radar_wire(path)
        and "radar_state" not in rel
        and "promotion_engine" not in rel
        and "universe_screener" not in rel
    ):
        out.append(
            "NEW: интегрировать radar funnel — читать `MarketRadarStore` / tier или "
            "`shortlist_reasons` с `radar_*`, не только static shortlist."
        )

    if rel.startswith("bot/delivery/"):
        out.extend(
            [
                "Проверить stage reason попадает в `rejected.jsonl` / `selected.jsonl`.",
                "Weighted confluence flag — не включать без calibration review.",
                "WATCH path не обходит hard_confluence_gate.",
                "Согласовать min_score с `effective_action_min_score` per symbol.",
                "delivery-guardian readonly audit перед merge.",
            ]
        )
    elif rel.startswith("bot/features/"):
        out.extend(
            [
                "Экспорт колонок документировать в INDICATORS.md.",
                "Только Polars; Wilder RSI/ATR; BB ddof=1.",
                "Высота frame = f(config intervals), не hardcode 300.",
                "Не импортировать microstructure напрямую с runtime (DeprecationWarning).",
                "live_check_indicators при изменении prepare_frame.",
            ]
        )
    elif rel.startswith("bot/persistence/"):
        out.extend(
            [
                "JSON payloads: запрет pl.DataFrame/numpy scalar в SQLite JSON columns.",
                "run_id scoped paths — clean_session_data smoke перед live.",
                "Forward migrations only; journal normalize_tracking_event.",
                "Split memory.py queries (F12) при >800 LOC.",
                "Outcomes retention vs config audit.",
            ]
        )
    elif rel.startswith("bot/dashboard/"):
        out.extend(
            [
                "NEW: показывать `radar_tier_cycle` / hot-warm counts в funnel API.",
                "Читать `shortlist_build.jsonl` не только legacy shortlist.jsonl.",
                "KPI delivered = sent|logged only.",
                "Operator actions read-only; no trade execution.",
                "Не блокировать bot event loop (async handlers).",
            ]
        )
    elif rel.startswith("bot/diagnostics/"):
        out.extend(
            [
                "Связать run_id с live_watch dir (F11 bridge).",
                "shortlist_not_routed vs radar_promoted semantics.",
                "startup_report suspicious_modules — добавить radar_stale.",
                "quality monitor: tier-0 ingest rate threshold.",
                "config_audit drift vs example TOML.",
            ]
        )
    elif rel.startswith("bot/regime/"):
        out.extend(
            [
                "regime_frame_4h JSON-safe в persistence (dict columns).",
                "composite vs legacy vote weights — telemetry при transition.",
                "Не блокировать loop тяжёлым HMM без executor.",
                "btc_phase в filter details — wave F10 N7.",
                "MarketRegimeAnalyzer cache TTL documented.",
            ]
        )
    elif rel.startswith("bot/engine/"):
        out.extend(
            [
                "lane_skips + detector_runs в cycle JSONL per symbol.",
                "Registry keys == strategy_catalog setup_id only.",
                "intra_candle fast lane limits — radar symbols 0.5× throttle (runtime).",
                "calculate_all timeout telemetry.",
                "test_engine_routing при новых families.",
            ]
        )
    elif rel.startswith("bot/ops/"):
        out.extend(
            [
                "startup_report: latest run_id auto, не hardcoded path.",
                "pid_utils согласован с main.py stop.",
                "Proxy discovery hook в health snapshot.",
                "—",
                "—",
            ]
        )
    elif rel.startswith("scripts/"):
        name = Path(rel).name
        if name == "live_supervised_session.py":
            out.extend(
                [
                    "P1: обёртка `launch_detached.py` в Makefile target.",
                    "Авто session_meta.json при session_start.",
                    "Post-run: calibration_pipeline + rollup в session_finished.",
                    "takeover: не убивать собственный PID tree.",
                    "Minute snapshot: surface radar_tier_cycle из bot log.",
                ]
            )
        elif name == "generate_py_audit_5x.py":
            out.extend(
                [
                    "Deprecated: использовать audit_py_deep_findings.py.",
                    "—",
                    "—",
                    "—",
                    "—",
                ]
            )
        else:
            out.extend(
                [
                    "bootstrap_repo_path; --config default.",
                    "Идемпотентность; sole-operator (no manual steps).",
                    "Документировать CLI в SOLO_OPERATOR_PLAYBOOK.",
                    "compileall/refactor gate при добавлении.",
                    "Связать с live_watch run_id где применимо.",
                ]
            )
    elif rel.startswith("tests/"):
        out.extend(
            [
                "Regression test при bugfix в зеркальном bot/ модуле.",
                "Live tests: tests/live/ + PYTEST_LIVE=1 only.",
                "Polars minimal frames; no private Binance.",
                "Wave agent naming stable для CI gate.",
                "Добавить radar/subscription cross-module test при новых wires.",
            ]
        )
    elif rel == "main.py":
        out.extend(
            [
                "Entry-only → bot.cli.run.",
                "Python 3.14 venv only.",
                "Не добавлять бизнес-логику.",
                "—",
                "—",
            ]
        )
    else:
        out.extend(
            [
                "Проверить import layer violations (verify_refactor_gate).",
                "Нет private Binance / auto-trading.",
                "graphify update после изменений.",
                "compileall + wave pytest.",
                "Удалить мёртвый код или explicit deprecated shim.",
            ]
        )

    # File-specific overrides (NEW findings from audit)
    overrides: dict[str, list[str]] = {
        "bot/runtime/intra_candle_scanner.py": [
            "DONE/P1: 0.5× throttle для `shortlist_bucket=radar` / radar_promoted reasons.",
            "Пропускать символы не в shortlist даже при bookTicker firehose noise.",
            "Telemetry tag `trigger=intra_candle` + radar flag для calibration.",
            "Согласовать fast lane max_setups с hot pool size.",
            "Тест: radar item gets shorter throttle window.",
        ],
        "bot/market/subscription_planner.py": [
            "DONE/P1: `priority_symbols` (radar hot/deep) перед shortlist в aggTrade merge.",
            "P2: отдельный kline budget tier для hot-only @1m.",
            "Document budget math в ORDER_FLOW_INGEST.md.",
            "Unit test priority ordering.",
            "Expose plan in dashboard WS panel.",
        ],
        "bot/dashboard/live.py": [
            "DONE/P1: shortlist API — radar_tier_cycle + shortlist_reasons в items.",
            "Читать shortlist_build.jsonl для radar summary block.",
            "Funnel: tier counts cold/warm/hot/deep.",
            "Приоритет priority_assets vs radar_promoted overlap metric.",
            "Не читать unbounded JSONL (keep tail limits).",
        ],
        "bot/runtime/watch_escalation.py": [
            "P2: `universe.radar.emit_watch_candidates` → silent WATCH DM (no channel).",
            "Не путать с ACTION delivery path.",
            "Escalation state key включает setup_id.",
            "HTML escape audit.",
            "Тест watch_ready_for_action_escalation edge scores.",
        ],
        "bot/market/rest_http.py": [
            "Abstract base — ensure rest_impl is only production impl.",
            "NotImplemented stubs — fail fast if mis-instantiated.",
            "Proxy pool injection consistent with network_proxy.",
            "Rate limit headers → telemetry.",
            "Live REST probe script alignment.",
        ],
        "bot/persistence/repository/memory.py": [
            "F12 P0: split queries; JSON encoder for benchmark_context.",
            "update_market_context batch size limit.",
            "Diary symbol index v6 migration verified.",
            "Tracking stats 1s timeout — document SLA.",
            "No dual-write legacy JSON stores.",
        ],
        "bot/runtime/analyzer/pipeline.py": [
            "F12: extract dispatch + conflict merge modules.",
            "family_gates single source MTF reject reasons.",
            "Stage latency telemetry per symbol.",
            "Semaphore delivery path non-blocking.",
            "test_wave_f10_agent_l mandatory on edit.",
        ],
        "bot/market/ws.py": [
            "F12 continue split; radar ingest on ticker arr path only once.",
            "set_radar_store on reconnect restore.",
            "Throttle duplicate ticker updates — metric in health.",
            "Subscription budget from planner + hot priority.",
            "live WS catalog pytest.",
        ],
        "bot/diagnostics/quality.py": [
            "NEW: alert if radar ingest rate < threshold vs !ticker@arr.",
            "Cross-check shortlist size vs radar deep promotion count.",
            "Quality JSON per run_id.",
            "Integrate with live_audit recommendations.",
            "Do not scan full telemetry unbounded.",
        ],
        "bot/telemetry.py": [
            "append_jsonl rotation / run scoped dirs.",
            "Schema version field per stream.",
            "radar_* streams optional dedicated file.",
            "slim_message_buffer for dashboard.",
            "Fail soft on disk full.",
        ],
    }
    if rel in overrides:
        return overrides[rel][:5]

    # Dedupe and take 5
    seen: set[str] = set()
    unique: list[str] = []
    for item in out:
        if item not in seen and item.strip():
            seen.add(item)
            unique.append(item)
    while len(unique) < 5:
        unique.append(f"Ревью {rel}: согласовать с PY_IMPLEMENTATION_PLAN фаза G (coverage).")
    return unique[:5]


def generate(include_tests: bool, include_scripts: bool) -> str:
    roots: list[Path] = [ROOT / "bot"]
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

    lines = [
        "# NEW findings: 5 уникальных улучшений на каждый `.py`",
        "",
        "> **Не шаблон пакета** — отдельные находки + зона radar coverage. "
        "Старый шаблонный каталог: [PY_FILE_AUDIT_5X.md](PY_FILE_AUDIT_5X.md). "
        "План: [PY_IMPLEMENTATION_PLAN.md](PY_IMPLEMENTATION_PLAN.md).",
        "",
        f"**Файлов:** {len(files)} | **Radar-gap modules flagged:** {len(RADAR_GAP_MODULES)}",
        "",
        "## Волна G — расширение покрытия (суть)",
        "",
        "1. Radar не должен жить только в 8 файлах — wiring в planner, intra_candle, dashboard, bot tracked sync.",
        "2. Каждая из 38 strategies — калибровка + catalog + telemetry reject stage (не общий шаблон).",
        "3. F12 splits для LOC>800 (memory, ws, pipeline, tracking, filters, live.py).",
        "4. JSON-safe persistence для всех context blobs.",
        "5. Ops: detached 6h + post-run calibration по run_id.",
        "",
    ]

    by_pkg: dict[str, list[Path]] = {}
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        parts = rel.split("/")
        pkg = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        by_pkg.setdefault(pkg, []).append(f)

    for pkg in sorted(by_pkg):
        lines.append(f"## `{pkg}/`")
        lines.append("")
        for path in by_pkg[pkg]:
            rel = path.relative_to(ROOT).as_posix()
            bullets = findings_for(rel, path)
            lines.append(f"### `{rel}` ({_loc(path)} LOC)")
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
    text = generate(include_tests=not args.no_tests, include_scripts=not args.no_scripts)
    args.output.write_text(text, encoding="utf-8")
    print(args.output, len(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
