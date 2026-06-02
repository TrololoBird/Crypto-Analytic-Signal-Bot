"""SQLite + parquet persistence (v9).

Boundaries:
- ``memory.py`` — active signals, outcomes, cooldowns (runtime writes)
- ``schema.py`` — record shapes / analysis schema
- ``cache.py`` — parquet time-series cache (read-heavy, no delivery side effects)
"""

from .cache import ParquetCache, TimeSeriesCache
from .memory import MemoryRepository
from .schema import SIGNAL_ANALYSIS_SCHEMA, OutcomeRecord, SignalRecord

__all__ = [
    "SIGNAL_ANALYSIS_SCHEMA",
    "MemoryRepository",
    "OutcomeRecord",
    "ParquetCache",
    "SignalRecord",
    "TimeSeriesCache",
]
