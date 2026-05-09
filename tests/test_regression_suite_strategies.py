# ruff: noqa: F401

from tests.remediation_regression_cases import (
    test_crowd_position_respects_strategy_family,
    test_cvd_divergence_respects_min_delta_threshold,
    test_ema_bounce_config_min_adx_changes_outcome,
    test_ema_bounce_emits_1h_timeframe,
    test_family_confirmation_soft_gates_missing_fast_context_when_strict,
    test_funding_reversal_defaults_cover_runtime_params,
    test_funding_reversal_runtime_params_gate_delta_and_stop,
    test_fvg_config_mitigation_threshold_changes_outcome,
    test_hidden_divergence_respects_rsi_and_delta_thresholds,
    test_squeeze_setup_runtime_params_drive_breakout_and_stop,
    test_structure_pullback_config_trend_threshold_changes_outcome,
    test_wick_trap_params_keep_backward_compatible_alias,
)
