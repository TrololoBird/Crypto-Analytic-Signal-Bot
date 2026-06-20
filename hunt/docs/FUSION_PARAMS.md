# Fusion engine — official parameters

> **Honest premise:** a threshold-free detector is impossible. Self-calibration removes
> *market-specific* magic numbers (RSI 66, OI z>2, fall 3%) and replaces them with
> *distribution-relative* statistics plus a **small, explicit** set of design parameters
> below. These are not hidden — they live in `config.defaults.toml [fusion]` and
> `hunt_core/detect/config.py`.

## Output semantics

| Field | Meaning | Not |
|-------|---------|-----|
| `fusion_score` | 0–100 directional strength index | Calibrated P(win) |
| `delivery_p_win` | From geometry / catalog EV recompute | Fusion logistic |
| `gate_open` | Vol-adjusted magnitude ≥ effective threshold AND PRE phase | Probability gate |

## Detection parameters

| Parameter | Default | Role |
|-----------|---------|------|
| `min_n` | 30 | Cold-start floor for robust-z / quantile |
| `lookback` | 120 | Trailing calibration window (bars) |
| `q_gate` | 0.92 | Symbol's own magnitude quantile for gate |
| `global_gate_floor` | 0.55 | `max(symbol_quantile, floor)` — flat-tape guard |
| `abs_magnitude_floor` | 0.5 | Minimum vol-adjusted magnitude before gate consult |
| `min_active_factors` | 2 | Directional factors required (no single-factor veto) |
| `vol_floor_pct` | 0.15 | ATR%% denominator floor for vol normalization |
| `fusion_score_scale` | 25 | Linear map magnitude → 0–100 score |

## Phase (CUSUM) parameters

| Parameter | Default | Role |
|-----------|---------|------|
| `cusum_k` | 0.5 | Drift slack in σ units on standardized returns |
| `cusum_span` | 96 | EWM span for return standardization |
| `q_phase` | 0.85 | \|CUSUM\| quantile = activation band |
| `phase_mid_exit_ratio` | 0.65 | Hysteresis: exit MID when \|CUSUM\| < band × ratio |
| `phase_mid_exit_bars` | 2 | Consecutive bars below exit band before PRE allowed |

## Factor-specific

| Parameter | Default | Role |
|-----------|---------|------|
| `funding_min_n` | 48 | Longer history for step-wise funding rate |
| `mad_epsilon` | 1e-6 | MAD scale floor (prevents z explosion) |
| `robust_z_clip` | 12 | Winsorize factor z-scores |

## Aggregation (not configurable yet)

Directional factors fuse by **signed median** + rank-vote agreement (not Stouffer Σz/√n).
Amplifiers use saturated `tanh`. Correlation shrinkage (PCA / inverse-variance) is future work.

## Replay harness (offline)

| Parameter | Default | Role |
|-----------|---------|------|
| `replay_warmup` | 60 | Bars before scoring |
| `replay_horizon_bars` | 16 | Forward outcome window |
| `replay_target_atr` | 1.5 | ATR multiple for first-touch hit |

Replay reports **ATR first-touch precision** plus **forward close return** and a naive
random-direction baseline. It is not out-of-sample unless run with `--walk-forward`.

## Preserved domain rules

Statistical fusion does not replace:

- `gate/_mission.py` — PRE-only watch TG
- Playbook N-of-M (`HUNT_PWIN_GATE=0` default)
- RR / levels / contract validation
- Maps confluence on catalog path

```text
fusion_score + PRE phase + mission + playbook + RR  →  deliver
```
