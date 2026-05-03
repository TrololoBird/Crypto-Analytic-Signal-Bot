# Strategy/Setup Audit — 2026-04-28

Scope: `bot/strategies/`, `bot/setups.py`, `bot/setups/`, `bot/models.py`, `config.toml`, `config.toml.example`.

Historical note: sections below the 2026-05-03 recheck preserve the original
2026-04-28 audit context. Where they conflict with the recheck, the recheck is
the current repository fact.

## 2026-05-03 recheck

Confirmed current state:

- Active import target for `bot.setups` is `bot/setups/__init__.py`, not the
  legacy same-name file `bot/setups.py`.
- Active `_build_signal(...)` already uses `normalize_trade_levels(...)`,
  finite checks, target normalization, `target_integrity_status`, and
  single-target metadata.
- `turtle_soup` no longer has the original TP contradiction: current code places
  long TP above entry and short TP below entry before calling `_build_signal`.
- `wick_trap_reversal` still starts from the swept level as TP1, but current code
  applies deterministic RR fallback before build when the swept level is too
  close; live strategy surface check on BTC/ETH/XAU/XAG produced no strategy
  errors.
- `session_killzone` pre-guard indexed access was fixed in this pass: the
  strategy now checks `work_15m.height < 20` and `time` column presence before
  reading `item(-1, "time")`.
- `session_killzone` killzone windows are now config-driven numeric parameters
  under `[bot.filters.setups.session_killzone]`, and `Overlap` is a first-class
  session window.
- `bos_choch` now records external stop-anchor diagnostics in rejection details:
  marker candidates, invalid markers/levels, side-filtered anchors, selected
  index/level when present.
- `bot.features` now emits `session_overlap` and `session_overlap_vol_20`, and
  keeps runtime indicator generation on the deterministic pure-Polars path even
  when `polars_ta` is installed.
- Regression checks passed after the fix:
  `tests/test_strategies.py`,
  `tests/test_regression_suite_setups_contracts.py`,
  `tests/test_regression_suite_contracts.py`, and
  `tests/test_regression_suite_tracking_delivery.py`.

Residual risks:

- The duplicate legacy file `bot/setups.py` remains in the tree and is stale
  relative to the active package implementation. It should be removed or made an
  explicit compatibility shim in a separate cleanup pass.
- Full SMC parity against an external reference is still not proven; current
  tests cover schema, length, selected invalidation behavior, and signal-level
  contracts rather than every ICT edge case.

## 1) Contract check: `setup_id`, `detect()` signature, expected input schema

### 1.1 Strategy contract compliance

Confirmed by static scan of all strategy classes:

- All active setup classes in `bot/strategies/*.py` expose class-level `setup_id`.
- All active setup classes implement `detect(self, prepared: PreparedSymbol, settings: BotSettings)`.
- All setup IDs are aligned between:
  - strategy classes,
  - `[bot.setups]` enable flags,
  - `[bot.filters.setups]` params map.

### 1.2 Runtime signal contract

- Strategies now build signals via shared `_build_signal(...)` helper (no direct `Signal(...)` calls in strategy files).
- `_build_signal(...)` populates all required `Signal` fields (`symbol`, `setup_id`, `direction`, `score`, `timeframe`, `entry_low/high`, `stop`, `take_profit_1/2`).

### 1.3 Expected input schema (effective)

Common assumptions across strategies (not centrally validated in one place):

- `prepared.work_15m` and `prepared.work_1h` are non-empty and include at least:
  - OHLC: `open`, `high`, `low`, `close`
  - indicators: `atr14`, `volume_ratio20`, often `rsi14`
  - for some strategies: `adx14`, `time`, `supertrend_dir`, `ema20`, `ema50`, `bb_pct_b`, swing-compatible OHLC history
- Optional: `prepared.work_4h` for extended TP logic in structural strategies.

Risk: many strategies guard bar count, but not always explicit column presence before `item(...)`.

## 2) Signal required fields + type consistency

### 2.1 Consistency

- Required fields are structurally consistent because `_build_signal` creates `Signal` uniformly.
- `timeframe`, `strategy_family`, `bias_4h`, `quote_volume`, `mark_price`, `volume_ratio` are populated consistently in helper.

### 2.2 Type caveats

- Historical finding: the legacy sibling file `bot/setups.py` only checked
  positive numeric values.
- Current active runtime fact: `bot/setups/__init__.py::_build_signal(...)`
  validates finite ATR/trade levels and normalizes targets before constructing
  `Signal`.

## 3) Guards: empty data, insufficient history, NaN/None

### 3.1 Good patterns observed

- Most strategies gate history (`w.height < N`) before core pattern logic.
- Most strategies reject `atr <= 0`; several also reject `math.isnan(atr)`.

### 3.2 Guard gaps / ordering issues

1. **Pre-guard indexed access in `session_killzone`**
   - Historical finding, fixed on 2026-05-03.
   - Regression: `test_session_killzone_rejects_empty_15m_before_time_lookup`.

2. **Non-finite value gap in shared `_build_signal`**
   - Historical finding for the legacy sibling file; not true for the active
     `bot/setups/__init__.py` runtime import target.

## 4) Logical defects (indexing / confirmation contradictions / RR)

### 4.1 Historical: `wick_trap_reversal` target contradiction

- Original finding is no longer current.
- Current code applies a deterministic RR fallback when the swept level is too
  close to `price_anchor`.

### 4.2 Historical: `turtle_soup` target contradiction

- Original finding is no longer current.
- Current code places long TP above entry and short TP below entry before
  calling `_build_signal(...)`.

### 4.3 RR semantics inconsistency

- Several strategies check RR vs `tp1` pre-build; others use graded penalty (`structure_break_retest`) and can pass with weak RR.
- This is by design in parts, but behavior is inconsistent cross-strategy and should be explicitly documented as intended policy.

## 5) Config vs actual parameter usage mismatches

### 5.1 `wick_trap_reversal`

Config supplies `sl_buffer_atr`, `min_rr`, `adx_penalty_factor`, etc., but detect path:

- Hardcodes SL buffer as `0.5 * ATR` (ignores `sl_buffer_atr`).
- Hardcodes RR floor `1.5` (ignores configurable `min_rr`).
- Hardcodes base score `0.60` (ignores configurable `base_score`).
- Uses `wick_through_atr_mult` and `closed_back_threshold` which are not surfaced in `config.toml` default block.
- `get_optimizable_params` defines `wick_atr_threshold`, but detect reads `wick_through_atr_mult` (naming drift).

### 5.2 `turtle_soup`

- `config.toml` includes `adx_penalty_factor`, but strategy defaults/detect do not consume it.

### 5.3 Cross-file consistency

- `config.toml` and `config.toml.example` are aligned with each other for setup keys.
- Misalignment is mainly between configured per-setup fields and what code actually reads/uses in select strategies.

## Priority remediation list

1. Historical TP construction contradictions in `wick_trap_reversal` and `turtle_soup`: fixed before/current as of 2026-05-03 recheck.
2. `session_killzone` `item(-1, "time")` pre-guard read: fixed on 2026-05-03.
3. Active `_build_signal` finite checks: present in `bot/setups/__init__.py`; legacy sibling remains stale.
4. Config contracts: `session_killzone` now exposes killzone hour parameters and overlap; `wick_trap_reversal`/`turtle_soup` still need a separate config-usage cleanup.
5. Shared preflight helper for required columns + min history remains a useful follow-up; do not bundle it with strategy behavior changes without targeted regressions.
