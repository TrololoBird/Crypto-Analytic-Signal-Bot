# Hunt Changelog (session notes)

## 2026-06-10 — Initial pump + mega leg + professional prompt

- **Extract:** `directional_filters.py` + `levels.fib_retracement_levels` — scoring filters / fib math out of watch monolith
- **Lifecycle:** `impulse_initiating`, `breakout_arming`, `mega_leg_continuation` (parabolic leg ≠ post_dump_bounce)
- **Early alerts:** PUMP/DUMP PREP/START; ignition bridge via `promote_initial_pump_lifecycle`
- **Dump:** ADX soft on exhaustion fade; early DUMP alerts before confirm
- **Forensic:** full BEAT pump to 8.3654 (not 4.27); JSONL gap May–Jun9 documented
- **Docs:** `docs/HUNT_IMPLEMENTER_PROMPT.md` v3 — canonical Claude prompt
- **Verify:** verify_logic 30/30

## 2026-06-10 — phase-hunt-impl-1 (forensic replay + phase-aware filters)
- jsonl_replay: recompute lifecycle FSM + long levels on stored ticks (stored phase = record-time code; flips now measurable). BEAT/VELVET window: 185× post_dump_bounce→distribution, 113× post_dump_bounce→impulse_initiating (VELVET mega-leg), 27× distribution→exhaustion_at_high (BEAT 6.14 top).
- alert_explain: `_hard_filter_blocks` — vwap_overbought/adx1h_uptrend soft on long@impulse_initiating/breakout_arming; adx1h_uptrend soft on short@exhaustion_at_high/distribution. Replay: BEAT exhaustion shorts gate-pass 52→129; VELVET impulse longs 0→7 (first 0.6976 @19:32Z).
- confirm_long in replay now receives lifecycle_phase (parity with live watch.py call).
- verify_logic 30/30; critical_audit BEAT/VELVET ok (both impulse_initiating live).

## 2026-06-10 — phase-hunt-impl-2 (calibration data + early-alert hygiene + live mark stream)
- signal_tracker: `entry_lifecycle_phase` immutable at open (lifecycle_phase мутировал каждый тик — фаза входа терялась); `close_lifecycle_phase` при закрытии.
- outcomes_report: таблица WR по entry phase × direction (первый прогон: post_dump_bounce short 0/3 -3.05%, post_dump_bounce long 2/0 +11.1%); lifecycle_stale/opposite_signal в LOSS_REASONS.
- early_alert: tier-hierarchy cooldown — start на cooldown глушит prep/imminent той же пары (replay: 76→68 would-sends).
- jsonl_replay: early_alert_simulation — would-send по tier'ам на recomputed lifecycle, общий cooldown-код с live.
- ws_feed: `!markPrice@arr@1s` — live mark/index/funding → market.{funding_live,basis_bps_live} (один stream на весь universe; synthetic parse verified, live ждёт proxy WS).
- verify_logic 30/30.

## 2026-06-11 — autonomous loop waves 2–14 (delivery gates + replay honesty + ops)

**North star:** tracker WR ≥70%, PnL growth. **Guardrails (n_tracker_closed < 30):** не снижать `confirm_min` / delivery fuel **72**; prep-shadow WR <50% → tighten держать.

### Delivery / alert_explain (W2–W5)
- `delivery_confluence_low` waiver: dump continuation shorts (`dump_active`/`distribution`, fall≥12%, structural dump hard, fuel≥min) — `min_struct_eff=1`.
- Bug fix: `_dump_continuation_short_ok` — убран redundant fuel re-check (блокировал confirmed shorts при fuel 64–71).
- Prep-shadow +3 fuel bump waived для confirmed structural dump shorts с fuel≥72.
- `_effective_min_rr()`: dump continuation shorts min R:R **1.10** (global 1.15).
- `_hard_filter_blocks`: adx1h_uptrend waived для short в `_DUMP_CONTINUATION_PHASES` (W11; replay 46%→72% gate-pass).

### Long path (W8–W10)
- `signal_engine.long_resistance_chase_veto()`: retest 0.5% если 5m closed above resistance, иначе chase floor 0.2%.
- `level_calibration.py`: +5% `sl_max_pct` для impulse/breakout hot mode.
- `levels._phase_min_rr_long()`: bounce 0.5, impulse 0.85, default 1.0.
- `watch._long_analysis`: `broke_resistance` только на **5m_closed**; intrabar → `live_above_resistance_unconfirmed` (+8 score only).

### Replay alignment (W6, W12)
- `jsonl_replay._replay_cal()` → `effective_hunt_params(symbol)` (confirm_min **72**, не defaults 60).
- `gate_lifecycle_phase()`: short gates → stored phase; long gates → recomputed pump phase over stale `distribution` (VELVET replay 0/2→2/2 gate).

### Ops / data plane (W13–W14)
- `resolve_tick_paths()`: daily archives + staging `dump_minute_watch.jsonl` (исправлен blind spot ~300 тиков).
- `watch.py`: periodic `rotate_hunt_ticks` каждые 10 min при staging ≥64KB.
- `scripts/hunt_boot_snapshot.py`: `latest_tick` meta; `scripts/hunt_journal.py`: autonomous journal helper.

### Metrics trajectory (replay + live)
| Metric | Baseline | Post W14 |
|--------|----------|----------|
| Short gate (replay) | 46% inflated | **~76%** (101/133) |
| verify_logic | 84/84 | **97/97** |
| verify_diff | 5/15 | **0–3/15** (premature only) |
| Tracker WR | 71.4% (n=7) | **85.7%** (n=7) — см. caveat ниже |
| prep_shadow WR | ~38% | **41%** (n=100) |

### Post-mortem: VELVETUSDT short @16:31 (thesis fail, paper win)
- Entry dump_active, `entry_lifecycle_bias=wait`, score 88, TG sent.
- MFE **~7.2%** vs TP1 need **~15.7%**; closed `bias_flip` (dump_active→post_dump_bounce) @+2.73%, **TP не достигнут**.
- Tracker считает **win** (structural exit + pnl>0.15%), но тезис провален.
- **Open:** блок TG/tracker open при `bias=wait` на dump_active short; отдельный счётчик `thesis_fail` для bias_flip без TP.

### Consciously NOT changed
- `confirm_min` / fuel floor **72** (n_tracker < 30).
- prep-shadow tighten при WR <50%.
- Delivery path order: contract → confluence → deliver.

### Verify
- verify_logic **97/97**; graphify updated.
