# Strategies Agent

## Scope

Work in `bot/strategies/` for individual setup detectors.

## Read Before Editing

- The target strategy file
- `bot/setup_base.py`
- `bot/domain/schemas.py`
- `bot/setups/__init__.py`
- `bot/setups/utils.py`
- Matching keys in `config.toml` and `config.toml.example`

## Strategy Contract

- Inherit from `BaseSetup`
- Keep a stable `setup_id`
- Implement `detect(prepared, settings) -> Signal | None`
- Implement `get_optimizable_params(settings | None) -> dict[str, float]`
- Return `bot.domain.schemas.Signal`, not ad hoc dicts or tuples
- Every strategy must be able to produce either a valid signal under its market
  conditions or a precise reject/skip reason. Silent non-participation is a bug.
- `experimental`, `beta`, and `observing:*` are labels only. They do not justify
  disabling, hiding, or bypassing a strategy. If a detector does not signal,
  inspect and repair its data contract, filters, confirmation model, and target
  logic.

## Local Rules

- `setup_id` must match the keys under `[bot.filters.setups]` and `[bot.setups]`.
- All registered setup flags should remain enabled unless the strategy is
  intentionally removed from `STRATEGY_CLASSES`. Do not disable a strategy
  because metadata says `experimental`, because current telemetry is negative,
  or because generated tests are weak.
- Prefer shared helpers over duplicating RR, SL/TP, scoring, or rejection logic.
- Guard frame height and required columns before `item(-1)`, `item(-2)`, or similar indexed access.
- Keep dataframe logic Polars-native. Use existing prepared Polars columns and
  installed `polars_ta`/Polars-derived indicator layers where available; do not
  hand-roll indicator math in a strategy when the feature layer already provides
  the column.
- Put tunable thresholds in config; hardcode only true invariants.
- If you add or rename a strategy, update `bot/strategies/__init__.py`, config files, and any registry/caller references in the same change.
- Keep signal fields consistent with `bot/domain/schemas.py` semantics: `direction`, `entry_low`, `entry_high`, `stop`, `take_profit_1`, `take_profit_2`.
- If a strategy needs enrichment (`funding_rate`, OI, depth, aggression,
  liquidation score, BTC context), declare it through metadata or explicit
  guarded rejects and verify the producing path in `bot/application/` or
  `bot/ws_manager.py`.
- Do not fake unavailable market data. A liquidation strategy should prefer
  `forceOrder`-derived liquidation context. If a REST-only diagnostic fallback
  uses an exhaustion proxy, label it explicitly in signal reasons and keep its
  score lower; never call proxy data `force_order`.
- When rewriting logic, check at least one real public GitHub implementation or
  official exchange/source documentation for the strategy/data concept. For
  Binance data fields, verify the actual public USD-M endpoint/stream and the
  producer path that fills `PreparedSymbol`.
- Generated tests are not proof that a strategy works. Do not modify generated
  tests to make a strategy look correct. Use compile/import checks, config
  validation, live/replay strategy decisions, and source-level comparison.
- Do not replace a broken hard reject with an always-on signal. Convert noisy
  context conflicts (orderbook, funding trend, 1h lag, volume below ideal) into
  explicit score penalties when the underlying trading setup remains valid.

## Token Discipline

- Compare against one or two similar strategies, not all 15.
- Read helpers first; many strategies share the same target-building and scoring patterns.

## Verification

- Check config key alignment and strategy exports after every strategy change.
- Use non-test proof first: compile/import checks, a local prepared-frame
  diagnostic, telemetry decision counts, and read-only live scripts. Generated
  tests are supplemental only and must not be treated as trading evidence.
- For "does not signal" bugs, report the dominant reason codes by setup before
  changing thresholds, then re-run a live/replay surface check and confirm there
  are no strategy errors or hidden skips.
