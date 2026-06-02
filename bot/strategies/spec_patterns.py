"""Re-export spec detector primitives from canonical ``bot.strategies._common``."""

from __future__ import annotations

from ._common import (
    SpecHit,
    as_float,
    as_int,
    build_spec_signal,
    current_utc_hour,
    finite_or_none,
    first_finite,
    last,
    previous,
    required_columns,
    with_spec_columns,
)

__all__ = [
    "SpecHit",
    "as_float",
    "as_int",
    "build_spec_signal",
    "current_utc_hour",
    "finite_or_none",
    "first_finite",
    "last",
    "previous",
    "required_columns",
    "with_spec_columns",
]
