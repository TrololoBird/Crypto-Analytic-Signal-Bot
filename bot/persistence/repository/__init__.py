"""SQLite + parquet persistence (v9).

Boundaries:
- ``memory.py`` — active signals, outcomes, cooldowns (runtime writes)
- ``schema.py`` — record shapes / analysis schema
- ``cache.py`` — parquet time-series cache (read-heavy, no delivery side effects)
"""

from .memory import MemoryRepository
from .schema import OutcomeRecord, SignalRecord, SIGNAL_ANALYSIS_SCHEMA
from .cache import ParquetCache, TimeSeriesCache

__all__ = [
    "MemoryRepository",
    "OutcomeRecord",
    "SignalRecord",
    "SIGNAL_ANALYSIS_SCHEMA",
    "ParquetCache",
    "TimeSeriesCache",
]
