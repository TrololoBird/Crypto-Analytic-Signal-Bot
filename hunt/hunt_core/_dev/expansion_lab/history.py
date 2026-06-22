"""Per-symbol block-score history — feeds deltas and persistence counters.

Pumps are born from *changes* and *duration*, not levels alone. This module keeps a
bounded in-memory ring buffer of recent block snapshots per symbol so the delta and
persistence layers can read trajectory ("compression 0.4→0.9 / 5d") and dwell time
("coil > 0.8 for 12 bars"). It is intentionally process-local and lossy: a cold start
simply yields zero deltas and zero persistence, never an error.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

_MAX_SAMPLES = 512


@dataclass(slots=True)
class HistorySample:
    ts: float
    scores: dict[str, float]


class ExpansionHistory:
    """Bounded ring buffer of block-score snapshots keyed by symbol."""

    def __init__(self, max_samples: int = _MAX_SAMPLES) -> None:
        self._max = max_samples
        self._store: dict[str, deque[HistorySample]] = {}
        self._lock = threading.Lock()

    def record(self, symbol: str, scores: dict[str, float], *, ts: float | None = None) -> None:
        sym = symbol.upper()
        sample = HistorySample(ts=ts if ts is not None else time.time(), scores=dict(scores))
        with self._lock:
            buf = self._store.get(sym)
            if buf is None:
                buf = deque(maxlen=self._max)
                self._store[sym] = buf
            buf.append(sample)

    def samples(self, symbol: str) -> list[HistorySample]:
        with self._lock:
            buf = self._store.get(symbol.upper())
            return list(buf) if buf else []

    def past_scores(self, symbol: str, *, lookback: int) -> dict[str, float] | None:
        """Block scores ``lookback`` samples ago (clamped to the oldest available)."""
        buf = self.samples(symbol)
        if not buf:
            return None
        idx = max(0, len(buf) - 1 - lookback)
        return dict(buf[idx].scores)

    def persistence_count(self, symbol: str, block: str, *, threshold: float) -> int:
        """Consecutive most-recent samples (incl. latest) with ``block`` >= threshold."""
        buf = self.samples(symbol)
        count = 0
        for sample in reversed(buf):
            if sample.scores.get(block, 0.0) >= threshold:
                count += 1
            else:
                break
        return count

    def clear(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._store.clear()
            else:
                self._store.pop(symbol.upper(), None)

    def snapshot(
        self,
        symbols: set[str] | None = None,
        *,
        max_samples: int = 40,
    ) -> dict[str, list[dict[str, float | str]]]:
        """Export recent samples for persistence (scores + ts only)."""
        cap = max(1, int(max_samples))
        out: dict[str, list[dict[str, float | str]]] = {}
        with self._lock:
            for sym, buf in self._store.items():
                if symbols is not None and sym not in symbols:
                    continue
                tail = list(buf)[-cap:]
                if not tail:
                    continue
                out[sym] = [{"ts": s.ts, "scores": dict(s.scores)} for s in tail]
        return out

    def restore(self, data: dict[str, Any] | None, *, max_samples: int = 512) -> None:
        """Restore from :meth:`snapshot` output."""
        if not isinstance(data, dict):
            return
        with self._lock:
            for sym, rows in data.items():
                if not isinstance(rows, list) or not rows:
                    continue
                buf: deque[HistorySample] = deque(maxlen=max_samples)
                for row in rows[-max_samples:]:
                    if not isinstance(row, dict):
                        continue
                    scores = row.get("scores")
                    if not isinstance(scores, dict):
                        continue
                    try:
                        ts = float(row.get("ts") or time.time())
                    except (TypeError, ValueError):
                        ts = time.time()
                    buf.append(HistorySample(ts=ts, scores={str(k): float(v) for k, v in scores.items()}))
                if buf:
                    self._store[str(sym).upper()] = buf


_GLOBAL_HISTORY = ExpansionHistory()


def global_history() -> ExpansionHistory:
    return _GLOBAL_HISTORY


__all__ = ["ExpansionHistory", "HistorySample", "global_history"]
