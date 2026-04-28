# Strategy/Setup Audit — 2026-04-28

Scope: `bot/strategies/`, `bot/setups.py`, `bot/setups/`, `bot/models.py`, `config.toml`, `config.toml.example`.

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

- `_build_signal` validates `<= 0`, but does **not** reject non-finite values (`NaN`, `inf`) for `stop/tp/atr/price_anchor`. This allows invalid numeric values to pass helper-level checks if upstream guards missed them.

## 3) Guards: empty data, insufficient history, NaN/None

### 3.1 Good patterns observed

- Most strategies gate history (`w.height < N`) before core pattern logic.
- Most strategies reject `atr <= 0`; several also reject `math.isnan(atr)`.

### 3.2 Guard gaps / ordering issues

1. **Pre-guard indexed access in `session_killzone`**
   - `prepared.work_15m.item(-1, "time")` is read before checking `w.height < 20`.
   - On empty/too-short frame this can raise before graceful reject path.

2. **Non-finite value gap in shared `_build_signal`**
   - Helper checks sign but not finite-ness, so `NaN` can bypass helper-level validation.

## 4) Logical defects (indexing / confirmation contradictions / RR)

### 4.1 Critical: `wick_trap_reversal` target contradiction (effectively no valid signals)

- Long branch requires `trig_close > level`, then sets `tp1 = level`.
- Short branch requires `trig_close < level`, then sets `tp1 = level`.
- `_build_signal` rejects long if `tp1 <= price_anchor`, and rejects short if `tp1 >= price_anchor`.
- Net effect: both branches are internally contradictory with helper validation, so setup is expected to reject all candidates at build stage.

### 4.2 Critical: `turtle_soup` target contradiction (effectively no valid signals)

- Long branch confirms close back above `rolling_low`, then sets `tp1 = rolling_low` (below/at entry).
- Short branch confirms close back below `rolling_high`, then sets `tp1 = rolling_high` (above/at entry).
- `_build_signal` enforces directional TP placement and rejects these constructions.
- Net effect: both directions are structurally invalid at signal build stage.

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

1. Fix TP construction contradictions in `wick_trap_reversal` and `turtle_soup` (highest priority).
2. Move `session_killzone` `item(-1, "time")` read below minimum-bar guard.
3. Harden `_build_signal` with finite checks (`math.isfinite`) for `atr`, `stop`, `tp1`, `tp2`, `price_anchor`.
4. Normalize config contracts for `wick_trap_reversal` and `turtle_soup` (remove dead params or wire them into logic).
5. Optionally introduce a shared preflight helper for required columns + min history to reduce duplicated guard gaps.
