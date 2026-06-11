"""Rolling signal quality monitor for setup-level delivery throttling.

The bot is signal-only, so this module never places orders and never touches
account state. It observes closed signal outcomes that the tracking subsystem
already computes, keeps a bounded rolling record per setup, and exposes a small
health contract that delivery can use before sending another signal.

The monitor is intentionally conservative:

* insufficient sample size never pauses a setup by itself;
* a hard pause needs either a clear loss streak or a poor win rate with enough
  samples;
* reduced confidence is reported before pause for early deterioration; and
* state is persisted on each update with an atomic replace so restarts keep the
  latest quality picture.

All public methods use a ``threading.Lock`` because tracking callbacks can be
invoked from executor-backed paths. No asyncio primitives are used here.
"""

from __future__ import annotations

import json
import logging
import math
import tempfile
import threading
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from engine.coercion import as_int
from engine.errors import DEFENSIVE_EXC

Recommendation = Literal["keep", "reduce_score", "pause"]

LOG = logging.getLogger("bot.quality_monitor")
DEFAULT_PERSIST_PATH = Path("data") / "bot" / "quality_monitor.json"
MIN_SAMPLES_FOR_REDUCE = 15
MIN_SAMPLES_FOR_PAUSE = 30
WIN_OUTCOMES = frozenset({"tp1_hit", "tp2_hit", "tp3_hit", "take_profit", "profit"})
LOSS_OUTCOMES = frozenset({"stop_loss", "liquidation", "loss"})
NEUTRAL_OUTCOMES = frozenset(
    {
        "expired",
        "expired_pending",
        "unactivated_close",
        "superseded",
        "cancelled",
        "canceled",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return default
    return numeric if math.isfinite(numeric) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _normalize_setup_id(setup_id: str) -> str:
    normalized = str(setup_id or "").strip()
    return normalized or "unknown"


def _normalize_symbol(symbol: str | None) -> str:
    return str(symbol or "").strip().upper()


def _is_win(outcome_result: str, r_multiple: float) -> bool:
    outcome = str(outcome_result or "").strip().lower()
    if outcome in WIN_OUTCOMES:
        return True
    if outcome in LOSS_OUTCOMES:
        return False
    return r_multiple > 0.0


def _is_loss(outcome_result: str, r_multiple: float) -> bool:
    outcome = str(outcome_result or "").strip().lower()
    if outcome in LOSS_OUTCOMES:
        return True
    if outcome in WIN_OUTCOMES:
        return False
    if outcome in NEUTRAL_OUTCOMES:
        return False
    return r_multiple < 0.0


def _record_is_trade(outcome_result: str, r_multiple: float) -> bool:
    outcome = str(outcome_result or "").strip().lower()
    if outcome in NEUTRAL_OUTCOMES:
        return False
    if outcome in WIN_OUTCOMES or outcome in LOSS_OUTCOMES:
        return True
    return r_multiple != 0.0


@dataclass(slots=True)
class QualityRecord:
    """Single closed signal outcome stored in the rolling quality window."""

    tracking_id: str
    setup_id: str
    outcome_result: str
    r_multiple: float
    recorded_at: str
    symbol: str = ""

    @property
    def is_trade(self) -> bool:
        return _record_is_trade(self.outcome_result, self.r_multiple)

    @property
    def is_win(self) -> bool:
        return self.is_trade and _is_win(self.outcome_result, self.r_multiple)

    @property
    def is_loss(self) -> bool:
        return self.is_trade and _is_loss(self.outcome_result, self.r_multiple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> QualityRecord:
        return cls(
            tracking_id=str(payload.get("tracking_id") or ""),
            setup_id=_normalize_setup_id(str(payload.get("setup_id") or "")),
            outcome_result=str(payload.get("outcome_result") or payload.get("result") or ""),
            r_multiple=_safe_float(payload.get("r_multiple"), 0.0),
            recorded_at=str(payload.get("recorded_at") or _utc_now_iso()),
            symbol=_normalize_symbol(str(payload.get("symbol") or "")),
        )


@dataclass(slots=True)
class SetupAggregate:
    """Persisted aggregate statistics for a setup.

    The aggregate is not the source of truth for rolling health decisions; the
    bounded records are. It exists for quick summaries, backward-compatible
    persistence, and continuity when older persisted files did not include every
    historical record.
    """

    setup_id: str
    sample_count: int = 0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    sum_r: float = 0.0
    sum_wins: float = 0.0
    sum_losses: float = 0.0
    consecutive_losses: int = 0
    last_outcome_result: str = ""
    last_r_multiple: float = 0.0
    last_updated_at: str = ""

    def update(self, record: QualityRecord) -> None:
        self.sample_count += 1
        self.last_outcome_result = record.outcome_result
        self.last_r_multiple = record.r_multiple
        self.last_updated_at = record.recorded_at
        if not record.is_trade:
            return
        self.trade_count += 1
        self.sum_r += record.r_multiple
        if record.is_win:
            self.win_count += 1
            self.sum_wins += record.r_multiple
            self.consecutive_losses = 0
        elif record.is_loss:
            self.loss_count += 1
            self.sum_losses += record.r_multiple
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def merge_record_window(self, records: list[QualityRecord]) -> None:
        self.sample_count = 0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.sum_r = 0.0
        self.sum_wins = 0.0
        self.sum_losses = 0.0
        self.consecutive_losses = 0
        self.last_outcome_result = ""
        self.last_r_multiple = 0.0
        self.last_updated_at = ""
        for record in records:
            self.update(record)

    @property
    def win_rate(self) -> float:
        if self.trade_count <= 0:
            return 0.0
        return self.win_count / self.trade_count

    @property
    def expectancy(self) -> float:
        if self.trade_count <= 0:
            return 0.0
        return self.sum_r / self.trade_count

    @property
    def avg_win_r(self) -> float:
        if self.win_count <= 0:
            return 0.0
        return self.sum_wins / self.win_count

    @property
    def avg_loss_r(self) -> float:
        if self.loss_count <= 0:
            return 0.0
        return self.sum_losses / self.loss_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "sample_count": self.sample_count,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "sum_r": round(self.sum_r, 8),
            "sum_wins": round(self.sum_wins, 8),
            "sum_losses": round(self.sum_losses, 8),
            "consecutive_losses": self.consecutive_losses,
            "last_outcome_result": self.last_outcome_result,
            "last_r_multiple": round(self.last_r_multiple, 8),
            "last_updated_at": self.last_updated_at,
            "win_rate": round(self.win_rate, 6),
            "expectancy_r": round(self.expectancy, 6),
            "avg_win_r": round(self.avg_win_r, 6),
            "avg_loss_r": round(self.avg_loss_r, 6),
        }

    @classmethod
    def from_dict(cls, setup_id: str, payload: dict[str, Any]) -> SetupAggregate:
        return cls(
            setup_id=_normalize_setup_id(str(payload.get("setup_id") or setup_id)),
            sample_count=_safe_int(payload.get("sample_count")),
            trade_count=_safe_int(payload.get("trade_count")),
            win_count=_safe_int(payload.get("win_count")),
            loss_count=_safe_int(payload.get("loss_count")),
            sum_r=_safe_float(payload.get("sum_r")),
            sum_wins=_safe_float(payload.get("sum_wins")),
            sum_losses=_safe_float(payload.get("sum_losses")),
            consecutive_losses=_safe_int(payload.get("consecutive_losses")),
            last_outcome_result=str(payload.get("last_outcome_result") or ""),
            last_r_multiple=_safe_float(payload.get("last_r_multiple")),
            last_updated_at=str(payload.get("last_updated_at") or ""),
        )


@dataclass(slots=True)
class SetupHealth:
    """Computed health result returned to callers."""

    setup_id: str
    win_rate: float
    expectancy_r: float
    sample_count: int
    trade_count: int
    is_healthy: bool
    consecutive_losses: int
    recommendation: Recommendation
    avg_r_multiple: float
    avg_win_r: float
    avg_loss_r: float
    last_outcome_result: str = ""
    last_r_multiple: float = 0.0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "setup_id": self.setup_id,
            "win_rate": round(self.win_rate, 6),
            "expectancy_r": round(self.expectancy_r, 6),
            "sample_count": self.sample_count,
            "trade_count": self.trade_count,
            "is_healthy": self.is_healthy,
            "consecutive_losses": self.consecutive_losses,
            "recommendation": self.recommendation,
            "avg_r_multiple": round(self.avg_r_multiple, 6),
            "avg_win_r": round(self.avg_win_r, 6),
            "avg_loss_r": round(self.avg_loss_r, 6),
            "last_outcome_result": self.last_outcome_result,
            "last_r_multiple": round(self.last_r_multiple, 6),
            "reasons": list(self.reasons),
        }
        if "insufficient_samples" in self.reasons or "insufficient_trade_samples" in self.reasons:
            payload["note"] = "insufficient_samples"
        return payload


@dataclass(slots=True)
class SymbolHealth:
    """Setup+symbol quality detail used for localized throttling decisions."""

    setup_id: str
    symbol: str
    consecutive_losses: int
    throttle: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "consecutive_losses": self.consecutive_losses,
            "throttle": self.throttle,
            "reason": self.reason,
        }


class SignalQualityMonitor:
    """Track rolling setup outcomes and recommend delivery throttles.

    Parameters
    ----------
    persist_path:
        JSON file used for durable monitor state. Defaults to
        ``data/bot/quality_monitor.json`` relative to the process working
        directory.
    window:
        Maximum number of outcome records retained per setup and in the global
        session window.
    pause_loss_streak:
        Consecutive loss threshold that immediately recommends ``pause``.
    pause_min_samples:
        Minimum trade sample count before poor win rate can pause a setup.
    reduce_min_samples:
        Minimum trade sample count before weak win rate recommends
        ``reduce_score``.
    """

    schema_version = 1

    def __init__(
        self,
        *,
        persist_path: str | Path | None = None,
        window: int = 200,
        pause_loss_streak: int = 5,
        pause_min_samples: int = MIN_SAMPLES_FOR_PAUSE,
        pause_win_rate: float = 0.25,
        reduce_min_samples: int = MIN_SAMPLES_FOR_REDUCE,
        reduce_win_rate: float = 0.40,
        symbol_cooldown_updates: int = 3,
    ) -> None:
        self.persist_path = Path(persist_path) if persist_path is not None else DEFAULT_PERSIST_PATH
        self.window = max(1, int(window))
        self.pause_loss_streak = max(1, int(pause_loss_streak))
        self.pause_min_samples = max(MIN_SAMPLES_FOR_PAUSE, int(pause_min_samples))
        self.pause_win_rate = max(0.0, min(float(pause_win_rate), 1.0))
        self.reduce_min_samples = max(MIN_SAMPLES_FOR_REDUCE, int(reduce_min_samples))
        self.reduce_win_rate = max(0.0, min(float(reduce_win_rate), 1.0))
        self.symbol_cooldown_updates = max(1, int(symbol_cooldown_updates))
        self._lock = threading.RLock()
        self._records_by_setup: dict[str, deque[QualityRecord]] = {}
        self._records_global: deque[QualityRecord] = deque(maxlen=self.window)
        self._aggregates: dict[str, SetupAggregate] = {}
        self._seen_tracking_ids: set[str] = set()
        self._symbol_loss_streaks: dict[tuple[str, str], int] = {}
        self._last_persist_error: str | None = None
        self._loaded = False
        self._load()

    def update(
        self,
        tracking_id: str,
        setup_id: str,
        outcome_result: str,
        r_multiple: float | None,
        *,
        symbol: str | None = None,
    ) -> None:
        """Record a closed signal outcome and persist monitor state.

        Duplicate ``tracking_id`` values are ignored so retrying a close event
        cannot double-count a trade. Neutral monitoring outcomes are retained
        for audit history but do not count toward win rate or expectancy.
        """
        normalized_setup = _normalize_setup_id(setup_id)
        normalized_tracking_id = str(tracking_id or "").strip()
        if not normalized_tracking_id:
            normalized_tracking_id = f"{normalized_setup}:{_utc_now_iso()}"
        record = QualityRecord(
            tracking_id=normalized_tracking_id,
            setup_id=normalized_setup,
            outcome_result=str(outcome_result or ""),
            r_multiple=_safe_float(r_multiple, 0.0),
            recorded_at=_utc_now_iso(),
            symbol=_normalize_symbol(symbol),
        )
        with self._lock:
            if record.tracking_id in self._seen_tracking_ids:
                LOG.debug(
                    "quality monitor duplicate update ignored | tracking_id=%s setup=%s",
                    record.tracking_id,
                    record.setup_id,
                )
                return
            self._seen_tracking_ids.add(record.tracking_id)
            setup_records = self._records_by_setup.setdefault(
                record.setup_id,
                deque(maxlen=self.window),
            )
            setup_records.append(record)
            self._records_global.append(record)
            aggregate = self._aggregates.setdefault(
                record.setup_id,
                SetupAggregate(setup_id=record.setup_id),
            )
            aggregate.merge_record_window(list(setup_records))
            self._update_symbol_streak(record)
            snapshot = self._state_snapshot_unlocked()
            self._persist_snapshot_unlocked(snapshot)

    def get_setup_health(self, setup_id: str) -> dict[str, Any]:
        """Return health fields for one setup id."""
        normalized = _normalize_setup_id(setup_id)
        with self._lock:
            return self._compute_health_unlocked(normalized).to_dict()

    def get_all_health(self) -> dict[str, dict[str, Any]]:
        """Return setup health for every setup seen in persisted or live state."""
        with self._lock:
            setup_ids = sorted(set(self._records_by_setup) | set(self._aggregates))
            return {
                setup_id: self._compute_health_unlocked(setup_id).to_dict()
                for setup_id in setup_ids
            }

    def get_session_summary(self) -> dict[str, Any]:
        """Return global rolling quality summary for recent closed outcomes."""
        with self._lock:
            records = [record for record in self._records_global if record.is_trade]
            total = len(records)
            wins = sum(1 for record in records if record.is_win)
            losses = sum(1 for record in records if record.is_loss)
            sum_r = sum(record.r_multiple for record in records)
            by_setup: dict[str, list[QualityRecord]] = {}
            for record in records:
                by_setup.setdefault(record.setup_id, []).append(record)
            setup_expectancies = {
                setup_id: sum(item.r_multiple for item in items) / max(len(items), 1)
                for setup_id, items in by_setup.items()
            }
            best_setup = (
                max(setup_expectancies.items(), key=lambda item: item[1])[0]
                if setup_expectancies
                else None
            )
            worst_setup = (
                min(setup_expectancies.items(), key=lambda item: item[1])[0]
                if setup_expectancies
                else None
            )
            return {
                "sample_count": len(self._records_global),
                "trade_count": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total, 6) if total else 0.0,
                "expectancy_r": round(sum_r / total, 6) if total else 0.0,
                "best_setup": best_setup,
                "worst_setup": worst_setup,
                "setup_count": len(set(self._records_by_setup) | set(self._aggregates)),
                "last_persist_error": self._last_persist_error,
            }

    def should_throttle_delivery(self, setup_id: str, symbol: str | None = None) -> bool:
        """Return True when delivery should be paused for setup/symbol quality.

        This combines the setup-level recommendation with a lightweight
        symbol-specific loss streak. The symbol streak only matters after a few
        consecutive losses for the same setup and symbol; it never throttles a
        setup that has not produced any closed trade outcomes.
        """
        normalized_setup = _normalize_setup_id(setup_id)
        normalized_symbol = _normalize_symbol(symbol)
        with self._lock:
            health = self._compute_health_unlocked(normalized_setup)
            if health.sample_count < MIN_SAMPLES_FOR_PAUSE:
                return False
            if health.recommendation == "pause":
                return True
            if normalized_symbol:
                streak = self._symbol_loss_streaks.get((normalized_setup, normalized_symbol), 0)
                return streak >= self.symbol_cooldown_updates
            return False

    def delivery_decision(self, setup_id: str, symbol: str | None = None) -> dict[str, Any]:
        """Return a delivery-facing decision payload."""
        health = self.get_setup_health(setup_id)
        throttle = self.should_throttle_delivery(setup_id, symbol)
        health["throttle"] = throttle
        if throttle and health["recommendation"] != "pause":
            health["recommendation"] = "pause"
            health["reasons"] = [*health.get("reasons", []), "symbol_loss_streak"]
        return health

    def reset_setup(self, setup_id: str) -> None:
        """Clear rolling state for a setup and persist the change."""
        normalized = _normalize_setup_id(setup_id)
        with self._lock:
            self._records_by_setup.pop(normalized, None)
            self._aggregates.pop(normalized, None)
            self._seen_tracking_ids = {
                record.tracking_id
                for records in self._records_by_setup.values()
                for record in records
            }
            self._records_global = deque(
                [record for records in self._records_by_setup.values() for record in records][
                    -self.window :
                ],
                maxlen=self.window,
            )
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())

    def recent_records(self, setup_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent records for diagnostics without exposing mutable state."""
        limit = max(1, int(limit))
        with self._lock:
            if setup_id is None:
                records = list(self._records_global)
            else:
                records = list(self._records_by_setup.get(_normalize_setup_id(setup_id), ()))
            return [record.to_dict() for record in records[-limit:]]

    def get_symbol_health(self, setup_id: str, symbol: str) -> dict[str, Any]:
        """Return localized setup+symbol streak health.

        A setup can be globally healthy while a single symbol is producing a
        poor sequence. Delivery can use this payload to explain localized
        throttles without implying that the whole detector is broken.
        """
        normalized_setup = _normalize_setup_id(setup_id)
        normalized_symbol = _normalize_symbol(symbol)
        with self._lock:
            losses = self._symbol_loss_streaks.get((normalized_setup, normalized_symbol), 0)
            throttle = losses >= self.symbol_cooldown_updates
            return SymbolHealth(
                setup_id=normalized_setup,
                symbol=normalized_symbol,
                consecutive_losses=losses,
                throttle=throttle,
                reason=(
                    f"symbol_consecutive_losses>={self.symbol_cooldown_updates}"
                    if throttle
                    else "ok"
                ),
            ).to_dict()

    def get_symbol_streaks(self) -> list[dict[str, Any]]:
        """Return all active symbol loss streaks sorted by severity."""
        with self._lock:
            rows = [
                {
                    "setup_id": setup_id,
                    "symbol": symbol,
                    "consecutive_losses": losses,
                    "throttle": losses >= self.symbol_cooldown_updates,
                }
                for (setup_id, symbol), losses in self._symbol_loss_streaks.items()
            ]
        rows.sort(
            key=lambda row: (
                as_int(row.get("consecutive_losses")),
                str(row["setup_id"]),
                str(row["symbol"]),
            ),
            reverse=True,
        )
        return rows

    def reset_symbol(self, setup_id: str, symbol: str) -> None:
        """Clear localized loss streak state for one setup+symbol pair."""
        normalized_setup = _normalize_setup_id(setup_id)
        normalized_symbol = _normalize_symbol(symbol)
        with self._lock:
            self._symbol_loss_streaks.pop((normalized_setup, normalized_symbol), None)
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())

    def record_batch(self, records: list[dict[str, Any]]) -> int:
        """Record a batch of historical outcomes.

        This is useful for local replay or migration code that wants to seed the
        monitor from already-closed outcomes. The method keeps duplicate
        tracking ids idempotent, persists once after the batch, and returns the
        number of accepted records.
        """
        accepted = 0
        with self._lock:
            for payload in records:
                if not isinstance(payload, dict):
                    continue
                record = QualityRecord.from_dict(payload)
                record.setup_id = _normalize_setup_id(record.setup_id)
                if not record.tracking_id:
                    record.tracking_id = f"{record.setup_id}:{record.recorded_at}:{accepted}"
                if record.tracking_id in self._seen_tracking_ids:
                    continue
                self._seen_tracking_ids.add(record.tracking_id)
                setup_records = self._records_by_setup.setdefault(
                    record.setup_id,
                    deque(maxlen=self.window),
                )
                setup_records.append(record)
                self._records_global.append(record)
                aggregate = self._aggregates.setdefault(
                    record.setup_id,
                    SetupAggregate(setup_id=record.setup_id),
                )
                aggregate.merge_record_window(list(setup_records))
                self._update_symbol_streak(record)
                accepted += 1
            if accepted:
                self._persist_snapshot_unlocked(self._state_snapshot_unlocked())
        return accepted

    def score_multiplier_for_setup(self, setup_id: str) -> float:
        """Return a conservative score multiplier derived from setup health.

        The delivery path currently hard-pauses only the ``pause``
        recommendation. This multiplier is exposed for future confluence or
        ranking code that wants to softly reduce deteriorating setups without
        changing the monitor's core recommendation thresholds.
        """
        health = self.get_setup_health(setup_id)
        recommendation = str(health.get("recommendation") or "keep")
        expectancy = _safe_float(health.get("expectancy_r"), 0.0)
        if recommendation == "pause":
            return 0.0
        if recommendation == "reduce_score":
            return 0.85 if expectancy >= 0.0 else 0.75
        if expectancy < -0.25 and int(health.get("trade_count") or 0) >= 8:
            return 0.92
        return 1.0

    def recommendation_counts(self) -> dict[str, int]:
        """Return count of setups by recommendation bucket."""
        counts: Counter[str] = Counter()
        for health in self.get_all_health().values():
            counts[str(health.get("recommendation") or "keep")] += 1
        return dict(counts)

    def unhealthy_setups(self) -> list[dict[str, Any]]:
        """Return setups that are not currently in the ``keep`` bucket."""
        rows = [
            health
            for health in self.get_all_health().values()
            if health.get("recommendation") != "keep"
        ]
        rows.sort(
            key=lambda row: (
                0 if row.get("recommendation") == "pause" else 1,
                -int(row.get("consecutive_losses") or 0),
                _safe_float(row.get("win_rate"), 0.0),
                str(row.get("setup_id") or ""),
            )
        )
        return rows

    def ranking_by_expectancy(self, *, min_trades: int = 1) -> list[dict[str, Any]]:
        """Return setup health rows ranked by expectancy descending."""
        rows = [
            health
            for health in self.get_all_health().values()
            if int(health.get("trade_count") or 0) >= max(1, int(min_trades))
        ]
        rows.sort(
            key=lambda row: (
                _safe_float(row.get("expectancy_r"), 0.0),
                _safe_float(row.get("win_rate"), 0.0),
                int(row.get("trade_count") or 0),
            ),
            reverse=True,
        )
        return rows

    def ranking_by_drawdown_pressure(self) -> list[dict[str, Any]]:
        """Return setup health rows ranked by current loss pressure."""
        rows = list(self.get_all_health().values())
        rows.sort(
            key=lambda row: (
                int(row.get("consecutive_losses") or 0),
                -_safe_float(row.get("expectancy_r"), 0.0),
                int(row.get("trade_count") or 0),
            ),
            reverse=True,
        )
        return rows

    def setup_report(self, setup_id: str, *, recent_limit: int = 10) -> dict[str, Any]:
        """Return health, recent records, and score multiplier for one setup."""
        normalized = _normalize_setup_id(setup_id)
        return {
            "health": self.get_setup_health(normalized),
            "score_multiplier": self.score_multiplier_for_setup(normalized),
            "recent_records": self.recent_records(normalized, limit=recent_limit),
        }

    def state_snapshot(self) -> dict[str, Any]:
        """Return the exact persisted state shape without writing it."""
        with self._lock:
            return self._state_snapshot_unlocked()

    def persist_now(self) -> None:
        """Persist the current state explicitly."""
        with self._lock:
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())

    def reload(self) -> None:
        """Reload monitor state from disk, replacing in-memory records."""
        with self._lock:
            self._records_by_setup.clear()
            self._records_global.clear()
            self._aggregates.clear()
            self._seen_tracking_ids.clear()
            self._symbol_loss_streaks.clear()
            self._loaded = False
            self._load_unlocked()

    def compact(self) -> None:
        """Rebuild aggregates from bounded records and drop orphan streaks."""
        with self._lock:
            active_pairs: set[tuple[str, str]] = set()
            for setup_id, records in list(self._records_by_setup.items()):
                trimmed = deque(list(records)[-self.window :], maxlen=self.window)
                self._records_by_setup[setup_id] = trimmed
                aggregate = self._aggregates.setdefault(
                    setup_id,
                    SetupAggregate(setup_id=setup_id),
                )
                aggregate.merge_record_window(list(trimmed))
                for record in trimmed:
                    if record.symbol:
                        active_pairs.add((record.setup_id, record.symbol))
            self._symbol_loss_streaks = {
                pair: losses
                for pair, losses in self._symbol_loss_streaks.items()
                if pair in active_pairs and losses > 0
            }
            self._records_global = deque(
                [record for records in self._records_by_setup.values() for record in records][
                    -self.window :
                ],
                maxlen=self.window,
            )
            self._seen_tracking_ids = {
                record.tracking_id
                for records in self._records_by_setup.values()
                for record in records
                if record.tracking_id
            }
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())

    def clear(self) -> None:
        """Clear all quality state and persist an empty monitor file."""
        with self._lock:
            self._records_by_setup.clear()
            self._records_global.clear()
            self._aggregates.clear()
            self._seen_tracking_ids.clear()
            self._symbol_loss_streaks.clear()
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())

    def explain_delivery(self, setup_id: str, symbol: str | None = None) -> dict[str, Any]:
        """Return a human-readable delivery explanation payload."""
        normalized_setup = _normalize_setup_id(setup_id)
        normalized_symbol = _normalize_symbol(symbol)
        health = self.get_setup_health(normalized_setup)
        symbol_health = (
            self.get_symbol_health(normalized_setup, normalized_symbol)
            if normalized_symbol
            else None
        )
        throttle = bool(
            health.get("recommendation") == "pause"
            or (symbol_health is not None and symbol_health.get("throttle"))
        )
        reasons: list[str] = []
        reasons.extend(str(item) for item in health.get("reasons", []) if item)
        if symbol_health is not None and symbol_health.get("throttle"):
            reasons.append(str(symbol_health.get("reason") or "symbol_throttle"))
        if not reasons:
            reasons.append("quality_ok")
        return {
            "setup_id": normalized_setup,
            "symbol": normalized_symbol,
            "throttle": throttle,
            "recommendation": "pause" if throttle else health.get("recommendation", "keep"),
            "score_multiplier": self.score_multiplier_for_setup(normalized_setup),
            "health": health,
            "symbol_health": symbol_health,
            "reasons": reasons,
        }

    def quality_flags_for_setup(self, setup_id: str) -> tuple[str, ...]:
        """Return compact string flags suitable for telemetry rows."""
        health = self.get_setup_health(setup_id)
        flags: list[str] = []
        recommendation = str(health.get("recommendation") or "keep")
        flags.append(f"quality:{recommendation}")
        trade_count = int(health.get("trade_count") or 0)
        if trade_count < self.reduce_min_samples:
            flags.append("quality:low_sample")
        if int(health.get("consecutive_losses") or 0) > 0:
            flags.append(f"quality:loss_streak:{health.get('consecutive_losses')}")
        expectancy = _safe_float(health.get("expectancy_r"), 0.0)
        if expectancy < 0.0:
            flags.append("quality:negative_expectancy")
        return tuple(flags)

    def export_json(self) -> str:
        """Serialize current state to a deterministic JSON string."""
        snapshot = self.state_snapshot()
        return json.dumps(snapshot, ensure_ascii=True, sort_keys=True, indent=2)

    def import_json(self, payload: str, *, merge: bool = True) -> int:
        """Import monitor records from a JSON state document.

        Returns the number of records accepted. When ``merge`` is false the
        current in-memory state is cleared before importing.
        """
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            LOG.warning("quality monitor import_json failed: invalid JSON")
            return 0
        if not isinstance(parsed, dict):
            return 0
        records_payload = parsed.get("records_by_setup", {})
        flattened: list[dict[str, Any]] = []
        if isinstance(records_payload, dict):
            for raw_records in records_payload.values():
                if isinstance(raw_records, list):
                    flattened.extend(item for item in raw_records if isinstance(item, dict))
        if not merge:
            self.clear()
        return self.record_batch(flattened)

    def setup_ids(self) -> tuple[str, ...]:
        """Return all setup ids known to the monitor."""
        with self._lock:
            return tuple(sorted(set(self._records_by_setup) | set(self._aggregates)))

    def has_setup(self, setup_id: str) -> bool:
        """Return true if a setup has any monitor state."""
        normalized = _normalize_setup_id(setup_id)
        with self._lock:
            return normalized in self._records_by_setup or normalized in self._aggregates

    def sample_count(self, setup_id: str | None = None) -> int:
        """Return total retained samples globally or for one setup."""
        with self._lock:
            if setup_id is None:
                return len(self._records_global)
            return len(self._records_by_setup.get(_normalize_setup_id(setup_id), ()))

    def trade_count(self, setup_id: str | None = None) -> int:
        """Return retained trade samples globally or for one setup."""
        with self._lock:
            if setup_id is None:
                return sum(1 for record in self._records_global if record.is_trade)
            return sum(
                1
                for record in self._records_by_setup.get(_normalize_setup_id(setup_id), ())
                if record.is_trade
            )

    def last_update_time(self, setup_id: str | None = None) -> str | None:
        """Return the last update timestamp globally or for one setup."""
        with self._lock:
            if setup_id is None:
                if not self._records_global:
                    return None
                return self._records_global[-1].recorded_at
            aggregate = self._aggregates.get(_normalize_setup_id(setup_id))
            return None if aggregate is None else aggregate.last_updated_at

    def delivery_table(self) -> list[dict[str, Any]]:
        """Return compact rows optimized for dashboard or telemetry display."""
        rows: list[dict[str, Any]] = []
        for setup_id, health in self.get_all_health().items():
            rows.append(
                {
                    "setup_id": setup_id,
                    "recommendation": health.get("recommendation"),
                    "win_rate": health.get("win_rate"),
                    "expectancy_r": health.get("expectancy_r"),
                    "trade_count": health.get("trade_count"),
                    "consecutive_losses": health.get("consecutive_losses"),
                    "score_multiplier": self.score_multiplier_for_setup(setup_id),
                }
            )
        rows.sort(
            key=lambda row: (
                0 if row["recommendation"] == "pause" else 1,
                0 if row["recommendation"] == "reduce_score" else 1,
                -int(row["consecutive_losses"] or 0),
                str(row["setup_id"]),
            )
        )
        return rows

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Return a small monitor summary safe to attach to heartbeat rows."""
        summary = self.get_session_summary()
        return {
            "quality_monitor": {
                "sample_count": summary["sample_count"],
                "trade_count": summary["trade_count"],
                "win_rate": summary["win_rate"],
                "expectancy_r": summary["expectancy_r"],
                "recommendations": self.recommendation_counts(),
                "unhealthy_setups": [row.get("setup_id") for row in self.unhealthy_setups()[:10]],
                "symbol_streaks": self.get_symbol_streaks()[:10],
                "last_persist_error": summary.get("last_persist_error"),
            }
        }

    def threshold_config(self) -> dict[str, Any]:
        """Return the active recommendation thresholds."""
        return {
            "window": self.window,
            "pause_loss_streak": self.pause_loss_streak,
            "pause_min_samples": self.pause_min_samples,
            "pause_win_rate": self.pause_win_rate,
            "reduce_min_samples": self.reduce_min_samples,
            "reduce_win_rate": self.reduce_win_rate,
            "symbol_cooldown_updates": self.symbol_cooldown_updates,
        }

    def outcome_counts(self, setup_id: str | None = None) -> dict[str, int]:
        """Return retained outcome counts globally or for one setup."""
        with self._lock:
            if setup_id is None:
                records = list(self._records_global)
            else:
                records = list(self._records_by_setup.get(_normalize_setup_id(setup_id), ()))
        counts: Counter[str] = Counter()
        for record in records:
            counts[str(record.outcome_result or "unknown")] += 1
        return dict(counts)

    def r_multiple_buckets(self, setup_id: str | None = None) -> dict[str, int]:
        """Return coarse R-multiple distribution buckets."""
        with self._lock:
            if setup_id is None:
                records = [record for record in self._records_global if record.is_trade]
            else:
                records = [
                    record
                    for record in self._records_by_setup.get(_normalize_setup_id(setup_id), ())
                    if record.is_trade
                ]
        buckets: Counter[str] = Counter()
        for record in records:
            r_value = record.r_multiple
            if r_value <= -1.0:
                buckets["<=-1R"] += 1
            elif r_value < 0.0:
                buckets["-1R..0R"] += 1
            elif r_value == 0.0:
                buckets["0R"] += 1
            elif r_value < 1.0:
                buckets["0R..1R"] += 1
            elif r_value < 2.0:
                buckets["1R..2R"] += 1
            else:
                buckets[">=2R"] += 1
        return dict(buckets)

    def equity_curve(self, setup_id: str | None = None) -> list[dict[str, Any]]:
        """Return cumulative R curve for retained trade outcomes."""
        with self._lock:
            if setup_id is None:
                records = [record for record in self._records_global if record.is_trade]
            else:
                records = [
                    record
                    for record in self._records_by_setup.get(_normalize_setup_id(setup_id), ())
                    if record.is_trade
                ]
        total = 0.0
        curve: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=1):
            total += record.r_multiple
            curve.append(
                {
                    "index": index,
                    "tracking_id": record.tracking_id,
                    "setup_id": record.setup_id,
                    "symbol": record.symbol,
                    "recorded_at": record.recorded_at,
                    "r_multiple": round(record.r_multiple, 6),
                    "cumulative_r": round(total, 6),
                }
            )
        return curve

    def drawdown_summary(self, setup_id: str | None = None) -> dict[str, Any]:
        """Return max drawdown over retained cumulative R outcomes."""
        curve = self.equity_curve(setup_id)
        peak = 0.0
        max_drawdown = 0.0
        max_drawdown_index = 0
        for point in curve:
            cumulative = _safe_float(point.get("cumulative_r"), 0.0)
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                max_drawdown_index = int(point.get("index") or 0)
        return {
            "setup_id": None if setup_id is None else _normalize_setup_id(setup_id),
            "points": len(curve),
            "max_drawdown_r": round(max_drawdown, 6),
            "max_drawdown_index": max_drawdown_index,
            "ending_cumulative_r": round(
                _safe_float(curve[-1].get("cumulative_r"), 0.0) if curve else 0.0,
                6,
            ),
        }

    def stability_score(self, setup_id: str) -> float:
        """Return a 0..1 stability score derived from health and drawdown."""
        health = self.get_setup_health(setup_id)
        trade_count = int(health.get("trade_count") or 0)
        if trade_count <= 0:
            return 0.5
        win_rate = _safe_float(health.get("win_rate"), 0.0)
        expectancy = _safe_float(health.get("expectancy_r"), 0.0)
        losses = int(health.get("consecutive_losses") or 0)
        drawdown = self.drawdown_summary(setup_id)
        drawdown_r = _safe_float(drawdown.get("max_drawdown_r"), 0.0)
        sample_factor = min(trade_count / max(self.pause_min_samples, 1), 1.0)
        expectancy_score = max(0.0, min(0.5 + expectancy / 4.0, 1.0))
        drawdown_penalty = min(drawdown_r / 8.0, 0.35)
        streak_penalty = min(losses / max(self.pause_loss_streak, 1), 1.0) * 0.30
        score = (
            win_rate * 0.40
            + expectancy_score * 0.35
            + sample_factor * 0.25
            - drawdown_penalty
            - streak_penalty
        )
        return round(max(0.0, min(score, 1.0)), 6)

    def setup_diagnostics(self, setup_id: str) -> dict[str, Any]:
        """Return a fuller diagnostic payload for one setup."""
        normalized = _normalize_setup_id(setup_id)
        return {
            "setup_id": normalized,
            "health": self.get_setup_health(normalized),
            "thresholds": self.threshold_config(),
            "outcome_counts": self.outcome_counts(normalized),
            "r_multiple_buckets": self.r_multiple_buckets(normalized),
            "drawdown": self.drawdown_summary(normalized),
            "stability_score": self.stability_score(normalized),
            "quality_flags": list(self.quality_flags_for_setup(normalized)),
            "recent_records": self.recent_records(normalized, limit=20),
        }

    def validate_state(self) -> list[str]:
        """Return state consistency warnings; empty means no obvious drift."""
        warnings: list[str] = []
        with self._lock:
            for setup_id, records in self._records_by_setup.items():
                if len(records) > self.window:
                    warnings.append(f"{setup_id}: records exceed window")
                aggregate = self._aggregates.get(setup_id)
                if aggregate is None:
                    warnings.append(f"{setup_id}: aggregate missing")
                    continue
                trade_count = sum(1 for record in records if record.is_trade)
                if aggregate.trade_count != trade_count:
                    warnings.append(
                        f"{setup_id}: aggregate trade_count={aggregate.trade_count} "
                        f"records={trade_count}"
                    )
                wins = sum(1 for record in records if record.is_win)
                if aggregate.win_count != wins:
                    warnings.append(f"{setup_id}: aggregate win_count drift")
            known_tracking_ids = {
                record.tracking_id
                for records in self._records_by_setup.values()
                for record in records
                if record.tracking_id
            }
            missing_seen = known_tracking_ids - self._seen_tracking_ids
            if missing_seen:
                warnings.append(f"seen tracking id index missing {len(missing_seen)} ids")
        return warnings

    def repair_state(self) -> list[str]:
        """Repair aggregate/index drift and return warnings observed before repair."""
        warnings = self.validate_state()
        with self._lock:
            for setup_id, records in self._records_by_setup.items():
                aggregate = self._aggregates.setdefault(
                    setup_id,
                    SetupAggregate(setup_id=setup_id),
                )
                aggregate.merge_record_window(list(records))
            self._seen_tracking_ids = {
                record.tracking_id
                for records in self._records_by_setup.values()
                for record in records
                if record.tracking_id
            }
            self._records_global = deque(
                [record for records in self._records_by_setup.values() for record in records][
                    -self.window :
                ],
                maxlen=self.window,
            )
            self._persist_snapshot_unlocked(self._state_snapshot_unlocked())
        return warnings

    def projected_health_after(
        self,
        setup_id: str,
        outcome_result: str,
        r_multiple: float,
    ) -> dict[str, Any]:
        """Return health projection if one hypothetical outcome were added."""
        normalized = _normalize_setup_id(setup_id)
        with self._lock:
            records = list(self._records_by_setup.get(normalized, ()))
        projected = QualityRecord(
            tracking_id=f"projection:{normalized}:{len(records)}",
            setup_id=normalized,
            outcome_result=str(outcome_result or ""),
            r_multiple=_safe_float(r_multiple, 0.0),
            recorded_at=_utc_now_iso(),
        )
        records.append(projected)
        records = records[-self.window :]
        aggregate = SetupAggregate(setup_id=normalized)
        aggregate.merge_record_window(records)
        recommendation, reasons = self._recommendation_for(aggregate)
        return SetupHealth(
            setup_id=normalized,
            win_rate=aggregate.win_rate,
            expectancy_r=aggregate.expectancy,
            sample_count=aggregate.sample_count,
            trade_count=aggregate.trade_count,
            is_healthy=recommendation == "keep",
            consecutive_losses=aggregate.consecutive_losses,
            recommendation=recommendation,
            avg_r_multiple=aggregate.expectancy,
            avg_win_r=aggregate.avg_win_r,
            avg_loss_r=aggregate.avg_loss_r,
            last_outcome_result=aggregate.last_outcome_result,
            last_r_multiple=aggregate.last_r_multiple,
            reasons=tuple(reasons),
        ).to_dict()

    def _update_symbol_streak(self, record: QualityRecord) -> None:
        if not record.symbol or not record.is_trade:
            return
        key = (record.setup_id, record.symbol)
        if record.is_loss:
            self._symbol_loss_streaks[key] = self._symbol_loss_streaks.get(key, 0) + 1
        else:
            self._symbol_loss_streaks.pop(key, None)

    def _compute_health_unlocked(self, setup_id: str) -> SetupHealth:
        records = list(self._records_by_setup.get(setup_id, ()))
        aggregate = self._aggregates.get(setup_id)
        if records:
            aggregate = SetupAggregate(setup_id=setup_id)
            aggregate.merge_record_window(records)
        elif aggregate is None:
            aggregate = SetupAggregate(setup_id=setup_id)
        recommendation, reasons = self._recommendation_for(aggregate)
        is_healthy = recommendation == "keep"
        return SetupHealth(
            setup_id=setup_id,
            win_rate=aggregate.win_rate,
            expectancy_r=aggregate.expectancy,
            sample_count=aggregate.sample_count,
            trade_count=aggregate.trade_count,
            is_healthy=is_healthy,
            consecutive_losses=aggregate.consecutive_losses,
            recommendation=recommendation,
            avg_r_multiple=aggregate.expectancy,
            avg_win_r=aggregate.avg_win_r,
            avg_loss_r=aggregate.avg_loss_r,
            last_outcome_result=aggregate.last_outcome_result,
            last_r_multiple=aggregate.last_r_multiple,
            reasons=tuple(reasons),
        )

    def _recommendation_for(self, aggregate: SetupAggregate) -> tuple[Recommendation, list[str]]:
        reasons: list[str] = []
        if aggregate.sample_count < self.reduce_min_samples:
            reasons.append("insufficient_samples")
            return "keep", reasons
        if (
            aggregate.sample_count >= self.pause_min_samples
            and aggregate.trade_count >= self.pause_min_samples
            and aggregate.consecutive_losses >= self.pause_loss_streak
        ):
            reasons.append(f"consecutive_losses>={self.pause_loss_streak}")
            return "pause", reasons
        if (
            aggregate.sample_count >= self.pause_min_samples
            and aggregate.trade_count >= self.pause_min_samples
            and aggregate.win_rate < self.pause_win_rate
        ):
            reasons.append(
                f"win_rate<{self.pause_win_rate:.2f}_after_{self.pause_min_samples}_samples"
            )
            return "pause", reasons
        if (
            aggregate.trade_count >= self.reduce_min_samples
            and aggregate.win_rate < self.reduce_win_rate
        ):
            reasons.append(
                f"win_rate<{self.reduce_win_rate:.2f}_after_{self.reduce_min_samples}_samples"
            )
            return "reduce_score", reasons
        if aggregate.trade_count == 0:
            reasons.append("insufficient_trade_samples")
        return "keep", reasons

    def _state_snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": _utc_now_iso(),
            "window": self.window,
            "records_by_setup": {
                setup_id: [record.to_dict() for record in records]
                for setup_id, records in sorted(self._records_by_setup.items())
            },
            "aggregates": {
                setup_id: aggregate.to_dict()
                for setup_id, aggregate in sorted(self._aggregates.items())
            },
            "symbol_loss_streaks": [
                {"setup_id": setup_id, "symbol": symbol, "losses": losses}
                for (setup_id, symbol), losses in sorted(self._symbol_loss_streaks.items())
            ],
            "session_summary": self._session_summary_unlocked(),
        }

    def _session_summary_unlocked(self) -> dict[str, Any]:
        trade_records = [record for record in self._records_global if record.is_trade]
        wins = sum(1 for record in trade_records if record.is_win)
        total = len(trade_records)
        setup_counts = Counter(record.setup_id for record in trade_records)
        return {
            "record_count": len(self._records_global),
            "trade_count": total,
            "win_rate": round(wins / total, 6) if total else 0.0,
            "expectancy_r": round(
                sum(record.r_multiple for record in trade_records) / total,
                6,
            )
            if total
            else 0.0,
            "setup_counts": dict(setup_counts),
        }

    def _persist_snapshot_unlocked(self, snapshot: dict[str, Any]) -> None:
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.persist_path.parent),
                delete=False,
                prefix=f".{self.persist_path.name}.",
                suffix=".tmp",
            ) as handle:
                tmp_name = handle.name
                json.dump(snapshot, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
            Path(tmp_name).replace(self.persist_path)
            self._last_persist_error = None
        except DEFENSIVE_EXC as exc:
            self._last_persist_error = str(exc)
            LOG.warning("quality monitor persist failed | path=%s error=%s", self.persist_path, exc)

    def _load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.persist_path.exists():
            return
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except DEFENSIVE_EXC as exc:
            self._last_persist_error = str(exc)
            LOG.warning("quality monitor load failed | path=%s error=%s", self.persist_path, exc)
            return
        records_payload = payload.get("records_by_setup", {})
        if isinstance(records_payload, dict):
            for setup_id, raw_records in records_payload.items():
                normalized_setup = _normalize_setup_id(str(setup_id))
                records = self._records_by_setup.setdefault(
                    normalized_setup,
                    deque(maxlen=self.window),
                )
                if not isinstance(raw_records, list):
                    continue
                for raw_record in raw_records[-self.window :]:
                    if not isinstance(raw_record, dict):
                        continue
                    record = QualityRecord.from_dict(raw_record)
                    record.setup_id = normalized_setup
                    records.append(record)
                    self._records_global.append(record)
                    if record.tracking_id:
                        self._seen_tracking_ids.add(record.tracking_id)
                    self._update_symbol_streak(record)
                aggregate = self._aggregates.setdefault(
                    normalized_setup,
                    SetupAggregate(setup_id=normalized_setup),
                )
                aggregate.merge_record_window(list(records))
        aggregates_payload = payload.get("aggregates", {})
        if isinstance(aggregates_payload, dict):
            for setup_id, raw_aggregate in aggregates_payload.items():
                if not isinstance(raw_aggregate, dict):
                    continue
                normalized_setup = _normalize_setup_id(str(setup_id))
                if normalized_setup not in self._aggregates:
                    self._aggregates[normalized_setup] = SetupAggregate.from_dict(
                        normalized_setup,
                        raw_aggregate,
                    )
        streaks = payload.get("symbol_loss_streaks", [])
        if isinstance(streaks, list):
            for item in streaks:
                if not isinstance(item, dict):
                    continue
                setup_id = _normalize_setup_id(str(item.get("setup_id") or ""))
                symbol = _normalize_symbol(str(item.get("symbol") or ""))
                losses = _safe_int(item.get("losses"))
                if setup_id and symbol and losses > 0:
                    self._symbol_loss_streaks[(setup_id, symbol)] = losses
        warnings = self.validate_state()
        if warnings:
            LOG.warning("quality_monitor_state_warnings", extra={"warnings": warnings})
            self.repair_state()
