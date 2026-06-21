# Verdict V2 Roadmap

Ship scope: **L0–L5 full stack** replacing `build_pinned_verdict` (Phases P1–P5).

## Shipped (P1–P5)

- **L1:** Seven engines + quality-weighted horizon blends A/B/C
- **L2:** HorizonTopology, conflict matrix, DisagreementState, MarketContext, MarketDriver, MaturityFeatures, DataQualityReport
- **L3:** Four pattern generators + top-3 resolver; JSONL audit at `data/verdict_v2_patterns.jsonl`
- **L4:** PathMapper → ExpectedPath, Catalyst, Fragility, SignalStrength, TradeQuality, `range_probability` on horizons
- **L5:** TradePlan, SignalDecision (LONG/SHORT/WAIT), 15m/5m timing gate (R14), pinned TG via `format_pinned_signal`
- **Config:** `[deep.verdict_v2.*]` in `config.defaults.toml` (env overrides still apply)
- **Calibration:** `hunt_core/_dev/calibrate_verdict_v2.py` → `data/verdict_v2_calibration.json`
- **Auto-tune:** `auto_tune_gates = true` reads calibration; `--apply` writes `data/verdict_v2_gate_overrides.json` (priority over auto-tune)

Entry: `build_scenario_verdict(row)` in `hunt_core/analysis/deep/verdict_v2/orchestrator.py`.

## V2.5 preview (shipped foundation)

- **SignalQueue TOP3** — `hunt_core/analysis/deep/verdict_v2/signal_queue.py`
- Opportunity score + **ACTIVE** (long/short) / **WAITING** (directional wait) lifecycle
- Persisted: `data/verdict_v2_signal_queue.json`
- **Activation zones** — `activation.py` (catalyst / entry proximity); exposed in `verdict_v2_summary.activation`
- **Batch TG delivery** — one message per deep loop cycle; hero = best rank; footer lists other updated symbols (`delivery_policy.format_cycle_peers_footer`)
- TG footer on deep pinned change (`signal_queue_tg_footer = true`)
- Probe: `hunt_core/_dev/probe_signal_queue.py`

## V2.5 (remaining — NOT ship)

- Multi-path `ForecastPath`
- `FutureSignalEngine` (full lifecycle product beyond proximity states)
- Cross-symbol delivery ranking beyond batch hero

## V3 (NOT ship)

- Historical Analog Engine → calibrated `SignalStrength.calibrated_p_win`
- Optional TG line: `(est. N% historical)`

## Non-negotiable rules (R1–R15)

See master plan checklist — enforced in unit synth (`hunt_core/_dev/check_verdict_v2.py`) and live smoke (`hunt_core/_dev/smoke_deep_pinned.py`).
