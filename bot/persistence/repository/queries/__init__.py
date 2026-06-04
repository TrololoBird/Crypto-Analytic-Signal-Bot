"""Read-only SQL query helpers for the persistence repository."""

from .outcomes import fetch_setup_stats_rows, fetch_signal_outcome_rows

__all__ = [
    "fetch_setup_stats_rows",
    "fetch_signal_outcome_rows",
]
