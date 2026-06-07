"""Parquet-based caching for time-series data."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOG = logging.getLogger("bot.persistence.repository.cache")


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ParquetCache:
    """Parquet-based cache for efficient time-series storage.

    Uses chunked storage by date for efficient append and query.
    Supports automatic compaction for old data.
    """

    def __init__(self, cache_dir: Path | str, max_chunk_days: int = 7):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_chunk_days = max_chunk_days

    def _hive_chunk_path(self, symbol: str, timeframe: str, date: datetime) -> Path:
        """Hive-style partition path for a single trading day."""
        return (
            self._cache_dir
            / f"symbol={symbol}"
            / f"timeframe={timeframe}"
            / f"date={date.strftime('%Y-%m-%d')}"
            / "data.parquet"
        )

    def _legacy_chunk_path(self, symbol: str, timeframe: str, date: datetime) -> Path:
        """Legacy flat filename kept for backward-compatible reads."""
        chunk_name = f"{symbol}_{timeframe}_{date.strftime('%Y%m%d')}.parquet"
        return self._cache_dir / chunk_name

    def _get_chunk_path(self, symbol: str, timeframe: str, date: datetime) -> Path:
        """Get path for a specific chunk (Hive layout)."""
        return self._hive_chunk_path(symbol, timeframe, date)

    def _get_chunk_pattern(self, symbol: str, timeframe: str) -> str:
        """Get glob pattern for legacy symbol/timeframe chunks."""
        return f"{symbol}_{timeframe}_*.parquet"

    def _hive_scan_paths(self, symbol: str, timeframe: str) -> list[str]:
        root = self._cache_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        if not root.exists():
            return []
        return [str(path) for path in sorted(root.rglob("data.parquet"))]

    def _write_parquet(self, frame: pl.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if frame.is_empty():
            return
        frame.lazy().sink_parquet(path, compression="zstd", statistics=True)

    def _merge_and_sink(
        self, existing_path: Path, incoming: pl.DataFrame, *, timestamp_col: str
    ) -> None:
        if existing_path.exists():
            merged = (
                pl.concat(
                    [pl.scan_parquet(str(existing_path)), incoming.lazy()],
                    how="diagonal_relaxed",
                )
                .unique(subset=[timestamp_col], keep="last")
                .sort(timestamp_col)
            )
        else:
            merged = incoming.lazy().unique(subset=[timestamp_col], keep="last").sort(timestamp_col)
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        merged.sink_parquet(existing_path, compression="zstd", statistics=True)

    def append(
        self,
        symbol: str,
        timeframe: str,
        df: pl.DataFrame,
        timestamp_col: str = "timestamp",
    ) -> None:
        """Append data to appropriate chunk(s).

        Data is partitioned by date for efficient storage.
        """
        if df.is_empty():
            return

        # Ensure timestamp column exists
        if timestamp_col not in df.columns:
            if "close_time" in df.columns:
                # Convert close_time (ms) to timestamp
                df = df.with_columns(
                    [(pl.col("close_time") / 1000).cast(pl.Datetime).alias(timestamp_col)]
                )
            elif "open_time" in df.columns:
                df = df.with_columns(
                    [(pl.col("open_time") / 1000).cast(pl.Datetime).alias(timestamp_col)]
                )

        # Group by date and write to separate chunks
        dates = df[timestamp_col].dt.date().unique().to_list()

        for date in dates:
            chunk_path = self._get_chunk_path(
                symbol, timeframe, datetime.combine(date, datetime.min.time())
            )

            # Filter data for this date
            day_df = df.filter(pl.col(timestamp_col).dt.date() == date)

            self._merge_and_sink(chunk_path, day_df, timestamp_col=timestamp_col)

        LOG.debug("Cached %d rows for %s/%s", len(df), symbol, timeframe)

    def read(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> pl.DataFrame:
        """Read cached data for symbol/timeframe.

        Args:
            symbol: Trading pair symbol
            timeframe: Time interval (1h, 4h, etc.)
            since: Start datetime (optional)
            until: End datetime (optional)

        Returns:
            Polars DataFrame with cached data
        """
        hive_paths = self._hive_scan_paths(symbol, timeframe)
        if hive_paths:
            lazy = pl.scan_parquet(hive_paths)
            if since is not None:
                lazy = lazy.filter(pl.col("timestamp") >= since)
            if until is not None:
                lazy = lazy.filter(pl.col("timestamp") <= until)
            return lazy.collect()

        pattern = self._get_chunk_pattern(symbol, timeframe)
        chunks = list(self._cache_dir.glob(pattern))

        if not chunks:
            return pl.DataFrame()

        lazy = pl.scan_parquet([str(chunk) for chunk in chunks])
        if since is not None:
            lazy = lazy.filter(pl.col("timestamp") >= since)
        if until is not None:
            lazy = lazy.filter(pl.col("timestamp") <= until)
        return lazy.collect().unique(keep="last").sort("timestamp")

    def read_recent(self, symbol: str, timeframe: str, lookback: timedelta) -> pl.DataFrame:
        """Read recent data for symbol/timeframe."""
        since = _utcnow_naive() - lookback
        return self.read(symbol, timeframe, since=since)

    def compact(self, max_age_days: int = 30) -> None:
        """Compact old chunks into monthly files.

        This reduces file count for old data while maintaining query performance.
        """
        cutoff = _utcnow_naive() - timedelta(days=max_age_days)
        monthly_groups: dict[tuple[str, str, str], list[Path]] = {}

        for chunk in self._cache_dir.glob("*.parquet"):
            try:
                parts = chunk.stem.split("_")
                if len(parts) < 3:
                    continue
                date_str = parts[-1]
                if len(date_str) != 8:
                    continue
                chunk_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
                if chunk_date >= cutoff:
                    continue
                symbol = parts[0]
                timeframe = "_".join(parts[1:-1])
                month_key = chunk_date.strftime("%Y%m")
                monthly_groups.setdefault((symbol, timeframe, month_key), []).append(chunk)
            except ValueError:
                continue

        compacted = 0
        for (symbol, timeframe, month_key), chunks in monthly_groups.items():
            monthly_path = self._cache_dir / f"{symbol}_{timeframe}_{month_key}.parquet"
            frames: list[pl.DataFrame] = []
            if monthly_path.exists():
                frames.append(pl.read_parquet(monthly_path))
            frames.extend(pl.read_parquet(chunk) for chunk in chunks)
            if not frames:
                continue
            merged = pl.concat(frames, how="diagonal_relaxed").unique(
                subset=["timestamp"],
                keep="last",
            )
            if "timestamp" in merged.columns:
                merged = merged.sort("timestamp")
            self._write_parquet(merged, monthly_path)
            for chunk in chunks:
                if chunk != monthly_path:
                    chunk.unlink(missing_ok=True)
            compacted += len(chunks)

        if compacted:
            LOG.info("Compacted %d old cache chunks into monthly parquet files", compacted)

    def clear(self, symbol: str | None = None) -> None:
        """Clear cache for symbol or all symbols."""
        if symbol:
            pattern = f"{symbol}_*.parquet"
            files = list(self._cache_dir.glob(pattern))
        else:
            files = list(self._cache_dir.glob("*.parquet"))

        for f in files:
            f.unlink()

        LOG.info("Cleared %d cache files", len(files))


class TimeSeriesCache:
    """In-memory LRU cache for recent time-series data with disk backing.

    Optimized for frequently accessed recent data.
    """

    def __init__(
        self,
        disk_cache: ParquetCache,
        max_symbols: int = 200,
        memory_bars: int = 500,  # Bars to keep in memory per symbol/timeframe
    ):
        self._disk = disk_cache
        self._max_symbols = max_symbols
        self._memory_bars = memory_bars
        self._memory: dict[str, pl.DataFrame] = {}
        self._access_times: dict[str, datetime] = {}

    def _make_key(self, symbol: str, timeframe: str) -> str:
        """Create cache key."""
        return f"{symbol}:{timeframe}"

    def get(self, symbol: str, timeframe: str, lookback_bars: int | None = None) -> pl.DataFrame:
        """Get data from cache (memory first, then disk)."""
        key = self._make_key(symbol, timeframe)
        bars = lookback_bars or self._memory_bars

        # Update access time
        self._access_times[key] = _utcnow_naive()

        # Check memory cache
        if key in self._memory:
            df = self._memory[key]
            if len(df) >= bars:
                return df.tail(bars)

        # Load from disk
        lookback = timedelta(hours=bars) if timeframe == "1h" else timedelta(days=bars)
        df = self._disk.read_recent(symbol, timeframe, lookback)

        # Store in memory (limited size)
        self._store_in_memory(key, df)

        return df.tail(bars) if len(df) > bars else df

    def put(self, symbol: str, timeframe: str, df: pl.DataFrame) -> None:
        """Store data in cache."""
        key = self._make_key(symbol, timeframe)

        # Store in memory
        self._store_in_memory(key, df)

        # Persist to disk
        self._disk.append(symbol, timeframe, df)

        self._access_times[key] = _utcnow_naive()

    def _store_in_memory(self, key: str, df: pl.DataFrame) -> None:
        """Store in memory with LRU eviction."""
        # Evict oldest if at capacity
        while len(self._memory) >= self._max_symbols and self._memory:
            oldest_key = min(
                self._access_times,
                key=lambda item_key: self._access_times.get(item_key, _utcnow_naive()),
            )
            del self._memory[oldest_key]
            del self._access_times[oldest_key]

        # Store truncated version
        if len(df) > self._memory_bars:
            self._memory[key] = df.tail(self._memory_bars)
        else:
            self._memory[key] = df

    def invalidate(self, symbol: str | None = None) -> None:
        """Invalidate cache entries."""
        if symbol:
            keys_to_remove = [k for k in self._memory if k.startswith(f"{symbol}:")]
            for key in keys_to_remove:
                del self._memory[key]
                del self._access_times[key]
        else:
            self._memory.clear()
            self._access_times.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        return {
            "memory_entries": len(self._memory),
            "max_entries": self._max_symbols,
            "memory_bars_per_entry": self._memory_bars,
        }


# ---------------------------------------------------------------------------
# Runtime candle storage and MTF helpers.
# ---------------------------------------------------------------------------
#
# These helpers extend the existing Parquet cache instead of introducing a
# parallel storage package.  They are public-data-only utilities for candles,
# resampling, and gap detection; they never place exchange orders.

TIMEFRAME_MILLISECONDS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

CANONICAL_CANDLE_SCHEMA: dict[str, Any] = {
    "open_time": pl.Int64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "close_time": pl.Int64,
    "quote_volume": pl.Float64,
    "trades": pl.Int64,
    "num_trades": pl.Int64,
    "taker_buy_base": pl.Float64,
    "taker_buy_quote": pl.Float64,
    "taker_buy_base_volume": pl.Float64,
    "taker_buy_quote_volume": pl.Float64,
    "is_closed": pl.Boolean,
    "source": pl.Utf8,
}


def normalize_cache_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{5,30}", normalized):
        msg = f"invalid symbol: {symbol!r}"
        raise ValueError(msg)
    return normalized


def normalize_cache_timeframe(timeframe: str) -> str:
    normalized = str(timeframe or "").strip().lower()
    if normalized not in TIMEFRAME_MILLISECONDS:
        msg = f"unsupported timeframe: {timeframe!r}"
        raise ValueError(msg)
    return normalized


def cache_timeframe_ms(timeframe: str) -> int:
    return TIMEFRAME_MILLISECONDS[normalize_cache_timeframe(timeframe)]


def _cache_ms_to_datetime(value: float) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=UTC)


def _cache_finite_float(value: object, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return default
    else:
        return default
    return numeric if math.isfinite(numeric) else default


def _cache_finite_int(value: object, *, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(float(value))
    except TypeError, ValueError:
        return default


def _empty_candle_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=CANONICAL_CANDLE_SCHEMA)


def _coerce_epoch_ms(series: pl.Series) -> pl.Series:
    dtype = series.dtype
    if dtype in (
        pl.Int8,
        pl.Int16,
        pl.Int32,
        pl.Int64,
        pl.UInt8,
        pl.UInt16,
        pl.UInt32,
        pl.UInt64,
        pl.Float32,
        pl.Float64,
    ):
        return series.cast(pl.Int64, strict=False)
    if getattr(dtype, "is_temporal", lambda: False)():
        return (series.cast(pl.Int64) // 1_000_000).cast(pl.Int64)
    parsed = series.str.strptime(pl.Datetime, strict=False)
    return (parsed.cast(pl.Int64) // 1_000_000).cast(pl.Int64)


def normalize_candle_frame(
    data: pl.DataFrame | Sequence[Mapping[str, Any]] | Mapping[str, Sequence[Any]],
    *,
    timeframe: str,
    source: str = "runtime",
    closed_only: bool = False,
) -> pl.DataFrame:
    """Return a canonical OHLCV frame suitable for Parquet storage."""

    tf = normalize_cache_timeframe(timeframe)
    frame = data.clone() if isinstance(data, pl.DataFrame) else pl.DataFrame(data)
    if frame.is_empty():
        return _empty_candle_frame()
    aliases = {
        "t": "open_time",
        "T": "close_time",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "q": "quote_volume",
        "n": "trades",
        "V": "taker_buy_base",
        "Q": "taker_buy_quote",
    }
    rename = {
        old: new
        for old, new in aliases.items()
        if old in frame.columns and new not in frame.columns
    }
    if rename:
        frame = frame.rename(rename)
    missing = [
        name
        for name in ("open_time", "open", "high", "low", "close", "volume")
        if name not in frame.columns
    ]
    if missing:
        msg = f"candle frame missing required columns: {missing}"
        raise ValueError(msg)
    frame = frame.with_columns(
        [
            _coerce_epoch_ms(frame["open_time"]).alias("open_time"),
            pl.col("open").cast(pl.Float64, strict=False).alias("open"),
            pl.col("high").cast(pl.Float64, strict=False).alias("high"),
            pl.col("low").cast(pl.Float64, strict=False).alias("low"),
            pl.col("close").cast(pl.Float64, strict=False).alias("close"),
            pl.col("volume").cast(pl.Float64, strict=False).alias("volume"),
        ]
    )
    if "close_time" not in frame.columns:
        frame = frame.with_columns(
            (pl.col("open_time") + cache_timeframe_ms(tf) - 1).alias("close_time")
        )
    else:
        frame = frame.with_columns(_coerce_epoch_ms(frame["close_time"]).alias("close_time"))
    for column, dtype in CANONICAL_CANDLE_SCHEMA.items():
        if column in frame.columns:
            frame = frame.with_columns(pl.col(column).cast(dtype, strict=False).alias(column))
        elif dtype == pl.Boolean:
            frame = frame.with_columns(pl.lit(value=True).alias(column))
        elif dtype == pl.Utf8:
            frame = frame.with_columns(pl.lit(source).alias(column))
        else:
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    if closed_only:
        frame = frame.filter(pl.col("is_closed").fill_null(value=True))
    frame = frame.filter(
        pl.col("open_time").is_not_null()
        & pl.col("open").is_finite()
        & pl.col("high").is_finite()
        & pl.col("low").is_finite()
        & pl.col("close").is_finite()
        & pl.col("volume").is_finite()
        & (pl.col("open") > 0.0)
        & (pl.col("high") > 0.0)
        & (pl.col("low") > 0.0)
        & (pl.col("close") > 0.0)
        & (pl.col("volume") >= 0.0)
    )
    ordered = [column for column in CANONICAL_CANDLE_SCHEMA if column in frame.columns]
    extras = [column for column in frame.columns if column not in ordered]
    return (
        frame.select(ordered + extras).unique(subset=["open_time"], keep="last").sort("open_time")
    )


@dataclass(frozen=True, slots=True)
class CandleGap:
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    missing_bars: int

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "missing_bars": self.missing_bars,
        }


@dataclass(frozen=True, slots=True)
class CandleCacheSummary:
    symbol: str
    timeframe: str
    rows: int
    start_ms: int | None
    end_ms: int | None
    source: str
    min_close: float | None = None
    max_close: float | None = None
    duplicates_removed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "rows": self.rows,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source": self.source,
            "min_close": self.min_close,
            "max_close": self.max_close,
            "duplicates_removed": self.duplicates_removed,
        }


@dataclass(frozen=True, slots=True)
class HotColdCacheConfig:
    base_dir: Path | str
    hot_rows: int = 2_000
    flush_rows: int = 240
    compression: str = "zstd"
    partition: str = "month"


@dataclass(slots=True)
class _HotCandleBuffer:
    rows: list[dict[str, Any]] = field(default_factory=list)
    append_count: int = 0

    def append(self, frame: pl.DataFrame) -> None:
        if frame.is_empty():
            return
        self.rows.extend(frame.to_dicts())
        self.append_count += frame.height

    def to_frame(self, *, timeframe: str) -> pl.DataFrame:
        if not self.rows:
            return _empty_candle_frame()
        return normalize_candle_frame(self.rows, timeframe=timeframe, source="hot")

    def clear(self) -> None:
        self.rows.clear()


class HotColdParquetCache:
    """Hot/cold candle cache using the existing memory-cache module."""

    def __init__(self, config: HotColdCacheConfig) -> None:
        self.config = config
        self.base_dir = Path(config.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._buffers: dict[tuple[str, str], _HotCandleBuffer] = defaultdict(_HotCandleBuffer)
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flush_count = 0

    def _key(self, symbol: str, timeframe: str) -> tuple[str, str]:
        return normalize_cache_symbol(symbol), normalize_cache_timeframe(timeframe)

    def _partition_path(self, symbol: str, timeframe: str, open_time: int) -> Path:
        dt = _cache_ms_to_datetime(open_time)
        root = self.base_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        if self.config.partition == "day":
            return (
                root
                / f"year={dt.year:04d}"
                / f"month={dt.month:02d}"
                / f"day={dt.day:02d}"
                / "candles.parquet"
            )
        if self.config.partition == "year":
            return root / f"year={dt.year:04d}" / "candles.parquet"
        return root / f"year={dt.year:04d}" / f"month={dt.month:02d}" / "candles.parquet"

    def _paths(self, symbol: str, timeframe: str) -> list[Path]:
        root = self.base_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        return sorted(root.rglob("*.parquet")) if root.exists() else []

    def _summary(
        self, symbol: str, timeframe: str, frame: pl.DataFrame, *, source: str, duplicates: int = 0
    ) -> CandleCacheSummary:
        if frame.is_empty():
            return CandleCacheSummary(
                symbol, timeframe, 0, None, None, source, duplicates_removed=duplicates
            )
        return CandleCacheSummary(
            symbol=symbol,
            timeframe=timeframe,
            rows=frame.height,
            start_ms=_cache_finite_int(frame["open_time"].min(), default=0) or 0,
            end_ms=_cache_finite_int(frame["open_time"].max(), default=0) or 0,
            source=source,
            min_close=_cache_finite_float(frame["close"].min()),
            max_close=_cache_finite_float(frame["close"].max()),
            duplicates_removed=duplicates,
        )

    async def append_frame(
        self,
        symbol: str,
        timeframe: str,
        frame: pl.DataFrame | Sequence[Mapping[str, Any]] | Mapping[str, Sequence[Any]],
        *,
        source: str = "runtime",
        flush: bool = False,
        closed_only: bool = False,
    ) -> CandleCacheSummary:
        symbol, timeframe = self._key(symbol, timeframe)
        normalized = normalize_candle_frame(
            frame, timeframe=timeframe, source=source, closed_only=closed_only
        )
        async with self._locks[(symbol, timeframe)]:
            buffer = self._buffers[(symbol, timeframe)]
            buffer.append(normalized)
            if len(buffer.rows) > self.config.hot_rows:
                buffer.rows = (
                    buffer.to_frame(timeframe=timeframe).tail(self.config.hot_rows).to_dicts()
                )
            if flush or len(buffer.rows) >= self.config.flush_rows:
                await self._flush_locked(symbol, timeframe)
        return self._summary(symbol, timeframe, normalized, source=source)

    async def _flush_locked(self, symbol: str, timeframe: str) -> CandleCacheSummary:
        buffer = self._buffers[(symbol, timeframe)]
        frame = buffer.to_frame(timeframe=timeframe)
        if frame.is_empty():
            return self._summary(symbol, timeframe, frame, source="flush")
        duplicates = 0
        for partition_value in frame["open_time"].unique().to_list():
            path = self._partition_path(symbol, timeframe, int(partition_value))

            def _same_partition(value: int, *, target: Path = path) -> bool:
                return self._partition_path(symbol, timeframe, int(value)) == target

            chunk = frame.filter(
                pl.col("open_time").map_elements(
                    _same_partition,
                    return_dtype=pl.Boolean,
                )
            )
            if chunk.is_empty():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                before = pl.scan_parquet(str(path)).select(pl.len()).collect().item() + chunk.height
                merged = (
                    pl.concat(
                        [pl.scan_parquet(str(path)), chunk.lazy()],
                        how="diagonal_relaxed",
                    )
                    .unique(subset=["open_time"], keep="last")
                    .sort("open_time")
                )
                duplicates += max(0, before - merged.select(pl.len()).collect().item())
            else:
                before = chunk.height
                merged = chunk.lazy().unique(subset=["open_time"], keep="last").sort("open_time")
                duplicates += max(0, before - merged.select(pl.len()).collect().item())
            merged.sink_parquet(
                path,
                compression=cast(
                    "Literal['lz4', 'uncompressed', 'snappy', 'gzip', 'brotli', 'zstd']",
                    self.config.compression,
                ),
                statistics=True,
            )
        buffer.clear()
        self._flush_count += 1
        return self._summary(symbol, timeframe, frame, source="flush", duplicates=duplicates)

    async def flush(self, symbol: str, timeframe: str) -> CandleCacheSummary:
        symbol, timeframe = self._key(symbol, timeframe)
        async with self._locks[(symbol, timeframe)]:
            return await self._flush_locked(symbol, timeframe)

    async def flush_all(self) -> list[CandleCacheSummary]:
        summaries: list[CandleCacheSummary] = []
        for symbol, timeframe in list(self._buffers):
            summaries.append(await self.flush(symbol, timeframe))
        return summaries

    async def read_window(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        include_hot: bool = True,
    ) -> pl.DataFrame:
        symbol, timeframe = self._key(symbol, timeframe)
        frames: list[pl.DataFrame] = []
        paths = self._paths(symbol, timeframe)
        if paths:
            lazy = pl.scan_parquet([str(path) for path in paths])
            if start_ms is not None:
                lazy = lazy.filter(pl.col("open_time") >= int(start_ms))
            if end_ms is not None:
                lazy = lazy.filter(pl.col("open_time") <= int(end_ms))
            frames.append(lazy.collect())
        if include_hot:
            async with self._locks[(symbol, timeframe)]:
                hot = self._buffers[(symbol, timeframe)].to_frame(timeframe=timeframe)
            if start_ms is not None:
                hot = hot.filter(pl.col("open_time") >= int(start_ms))
            if end_ms is not None:
                hot = hot.filter(pl.col("open_time") <= int(end_ms))
            if not hot.is_empty():
                frames.append(hot)
        if not frames:
            return _empty_candle_frame()
        return (
            pl.concat(frames, how="diagonal_relaxed")
            .unique(subset=["open_time"], keep="last")
            .sort("open_time")
        )

    async def latest(self, symbol: str, timeframe: str, *, limit: int) -> pl.DataFrame:
        if limit <= 0:
            return _empty_candle_frame()
        return (await self.read_window(symbol, timeframe)).tail(limit)

    async def detect_gaps(self, symbol: str, timeframe: str) -> list[CandleGap]:
        frame = await self.read_window(symbol, timeframe)
        return detect_candle_gaps(frame, symbol=symbol, timeframe=timeframe)

    def stats(self) -> dict[str, object]:
        hot = {
            f"{symbol}:{timeframe}": len(buffer.rows)
            for (symbol, timeframe), buffer in self._buffers.items()
            if buffer.rows
        }
        return {
            "base_dir": str(self.base_dir),
            "hot_buffers": hot,
            "hot_rows": sum(hot.values()),
            "flush_count": self._flush_count,
            "partition": self.config.partition,
            "compression": self.config.compression,
        }


def detect_candle_gaps(frame: pl.DataFrame, *, symbol: str, timeframe: str) -> list[CandleGap]:
    tf = normalize_cache_timeframe(timeframe)
    if frame.height < 2:
        return []
    step = cache_timeframe_ms(tf)
    times = [int(value) for value in frame.sort("open_time")["open_time"].to_list()]
    gaps: list[CandleGap] = []
    previous = times[0]
    for current in times[1:]:
        if current - previous > step:
            gaps.append(
                CandleGap(
                    symbol=normalize_cache_symbol(symbol),
                    timeframe=tf,
                    start_ms=previous + step,
                    end_ms=current - step,
                    missing_bars=max(0, int((current - previous) // step) - 1),
                )
            )
        previous = current
    return gaps


def resample_ohlcv_frame(
    frame: pl.DataFrame,
    *,
    source_timeframe: str,
    target_timeframe: str,
    closed_only: bool = True,
) -> pl.DataFrame:
    source_tf = normalize_cache_timeframe(source_timeframe)
    target_tf = normalize_cache_timeframe(target_timeframe)
    source_minutes = cache_timeframe_ms(source_tf) // 60_000
    target_minutes = cache_timeframe_ms(target_tf) // 60_000
    if target_minutes < source_minutes or target_minutes % source_minutes != 0:
        msg = f"cannot resample {source_tf} to {target_tf}"
        raise ValueError(msg)
    source = normalize_candle_frame(frame, timeframe=source_tf, source="resample")
    if source.is_empty() or source_tf == target_tf:
        return source
    expected = max(1, target_minutes // source_minutes)
    result = (
        source.with_columns(pl.from_epoch(pl.col("open_time"), time_unit="ms").alias("_dt"))
        .sort("_dt")
        .group_by_dynamic(
            "_dt",
            every=f"{target_minutes}m",
            period=f"{target_minutes}m",
            closed="left",
            label="left",
        )
        .agg(
            [
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("volume").sum().alias("volume"),
                pl.col("quote_volume").sum().alias("quote_volume"),
                pl.col("trades").sum().alias("trades"),
                pl.col("num_trades").sum().alias("num_trades"),
                pl.col("taker_buy_base").sum().alias("taker_buy_base"),
                pl.col("taker_buy_quote").sum().alias("taker_buy_quote"),
                pl.len().alias("bar_count"),
                pl.col("source").last().alias("source"),
            ]
        )
        .with_columns(
            [
                (pl.col("_dt").cast(pl.Int64) // 1_000).alias("open_time"),
                (pl.col("_dt").cast(pl.Int64) // 1_000 + cache_timeframe_ms(target_tf) - 1).alias(
                    "close_time"
                ),
                (pl.col("bar_count") >= expected).alias("is_closed"),
            ]
        )
        .drop("_dt")
        .sort("open_time")
    )
    if closed_only:
        result = result.filter(pl.col("is_closed"))
    return normalize_candle_frame(result, timeframe=target_tf, source="resample", closed_only=False)


def build_mtf_frames(
    frame: pl.DataFrame,
    *,
    source_timeframe: str = "1m",
    target_timeframes: Sequence[str] = ("5m", "15m", "1h", "4h"),
) -> dict[str, pl.DataFrame]:
    return {
        normalize_cache_timeframe(tf): resample_ohlcv_frame(
            frame,
            source_timeframe=source_timeframe,
            target_timeframe=tf,
        )
        for tf in target_timeframes
    }


def htf_trend_label(
    frame: pl.DataFrame, *, ema_period: int = 50, threshold_pct: float = 0.2
) -> str:
    if frame.height < ema_period + 5:
        return "neutral"
    work = frame.with_columns(pl.col("close").ewm_mean(span=ema_period, adjust=False).alias("_ema"))
    close = _cache_finite_float(work.item(-1, "close"), default=0.0) or 0.0
    ema = _cache_finite_float(work.item(-1, "_ema"), default=close) or close
    if ema <= 0.0:
        return "neutral"
    diff_pct = (close - ema) / ema * 100.0
    if diff_pct >= threshold_pct:
        return "bullish"
    if diff_pct <= -threshold_pct:
        return "bearish"
    return "neutral"


def signal_allowed_by_mtf(
    direction: str, mtf_frames: Mapping[str, pl.DataFrame]
) -> tuple[bool, str]:
    normalized = str(direction or "").strip().lower()
    if normalized not in {"long", "short"}:
        return False, "invalid_direction"
    conflicts: list[str] = []
    confirmations: list[str] = []
    for timeframe in ("1h", "4h"):
        frame = mtf_frames.get(timeframe)
        if frame is None or frame.is_empty():
            continue
        trend = htf_trend_label(frame)
        if normalized == "long" and trend == "bearish":
            conflicts.append(f"{timeframe}:bearish")
        elif normalized == "short" and trend == "bullish":
            conflicts.append(f"{timeframe}:bullish")
        elif trend != "neutral":
            confirmations.append(f"{timeframe}:{trend}")
    if conflicts:
        return False, "htf_conflict:" + ",".join(conflicts)
    return True, "htf_confirmed:" + ",".join(confirmations) if confirmations else "htf_neutral"
