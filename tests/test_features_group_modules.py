from __future__ import annotations

import polars as pl

from bot import features, features_advanced, features_core, features_oscillators


def _frame() -> pl.DataFrame:
    n = 80
    return pl.DataFrame(
        {
            "open": [100.0 + i * 0.1 for i in range(n)],
            "high": [101.0 + i * 0.1 for i in range(n)],
            "low": [99.0 + i * 0.1 for i in range(n)],
            "close": [100.5 + i * 0.1 for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        }
    )


def test_core_wrappers_delegate_to_core_module() -> None:
    df = _frame()

    assert features._atr(df, 14).equals(
        features_core.atr(df, 14, plta=features.plta, has_talib=features._HAS_TALIB)
    )
    assert features._adx(df, 14).equals(
        features_core.adx(df, 14, plta=features.plta, has_talib=features._HAS_TALIB)
    )
    assert features._vwap(df).equals(features_core.vwap(df))
    assert features._roc(df, 10).equals(
        features_core.roc(df, 10, plta=features.plta, has_talib=features._HAS_TALIB)
    )


def test_advanced_and_oscillator_wrappers_delegate_to_group_modules() -> None:
    df = _frame()

    st_a, st_dir_a = features._supertrend(df, 10, 3.0)
    st_b, st_dir_b = features_advanced.supertrend(df, 10, 3.0)
    assert st_a.equals(st_b)
    assert st_dir_a.equals(st_dir_b)

    k_a, d_a = features._stochastic(df)
    k_b, d_b = features_oscillators.stochastic(df)
    assert k_a.equals(k_b)
    assert d_a.equals(d_b)
