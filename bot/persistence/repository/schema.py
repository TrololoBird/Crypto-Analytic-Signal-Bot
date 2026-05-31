"""Repository record types and analysis parquet schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import polars as pl

SIGNAL_ANALYSIS_SCHEMA = {
    "signal_id": pl.String,
    "symbol": pl.String,
    "strategy_id": pl.String,
    "direction": pl.String,
    "entry_price": pl.Float64,
    "stop_loss": pl.Float64,
    "take_profit_1": pl.Float64,
    "take_profit_2": pl.Float64,
    "score": pl.Float64,
    "created_at": pl.String,
    "timeframe": pl.String,
    "atr_pct": pl.Float64,
    "spread_bps": pl.Float64,
    "rsi_1h": pl.Float64,
    "adx_1h": pl.Float64,
    "volume_ratio": pl.Float64,
    "funding_rate": pl.Float64,
    "oi_change_pct": pl.Float64,
    "features": pl.String,
    "metadata": pl.String,
    "outcome_id": pl.String,
    "result": pl.String,
    "pnl_24h": pl.Float64,
    "max_profit_pct": pl.Float64,
    "max_loss_pct": pl.Float64,
}


@dataclass
class SignalRecord:
    """Record of generated signal."""

    signal_id: str
    symbol: str
    strategy_id: str
    direction: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    score: float
    take_profit_3: float | None = None
    valid_until: datetime | None = None
    scale_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    timeframe: str = "1h"
    atr_pct: float = 0.0
    spread_bps: float = 0.0
    rsi_1h: float | None = None
    adx_1h: float | None = None
    volume_ratio: float | None = None
    funding_rate: float | None = None
    oi_change_pct: float | None = None

    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        if self.valid_until is not None:
            result["valid_until"] = self.valid_until.isoformat()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignalRecord:
        if "created_at" in data and isinstance(data["created_at"], str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if "valid_until" in data and isinstance(data["valid_until"], str):
            data["valid_until"] = datetime.fromisoformat(data["valid_until"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def validate(self) -> None:
        if not self.signal_id:
            raise ValueError("signal_id cannot be empty")
        if not self.symbol:
            raise ValueError("symbol cannot be empty")
        if self.entry_price <= 0:
            raise ValueError(f"invalid entry_price: {self.entry_price}")
        if self.stop_loss <= 0:
            raise ValueError(f"invalid stop_loss: {self.stop_loss}")
        if self.take_profit_1 <= 0:
            raise ValueError(f"invalid take_profit_1: {self.take_profit_1}")
        if self.take_profit_2 <= 0:
            raise ValueError(f"invalid take_profit_2: {self.take_profit_2}")
        if self.take_profit_3 is not None and self.take_profit_3 <= 0:
            raise ValueError(f"invalid take_profit_3: {self.take_profit_3}")


@dataclass
class OutcomeRecord:
    """Record of signal outcome after tracking."""

    outcome_id: str
    signal_id: str
    symbol: str

    price_1h: float | None = None
    price_4h: float | None = None
    price_24h: float | None = None

    pnl_1h: float | None = None
    pnl_4h: float | None = None
    pnl_24h: float | None = None

    max_profit_pct: float = 0.0
    max_loss_pct: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0

    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_sl: bool = False
    result: str = ""

    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None

    time_to_tp1_min: int | None = None
    time_to_tp2_min: int | None = None
    time_to_sl_min: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.updated_at:
            result["updated_at"] = self.updated_at.isoformat()
        if self.closed_at:
            result["closed_at"] = self.closed_at.isoformat()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutcomeRecord:
        if "updated_at" in data and isinstance(data["updated_at"], str):
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        if "closed_at" in data and isinstance(data["closed_at"], str):
            data["closed_at"] = datetime.fromisoformat(data["closed_at"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def validate(self) -> None:
        if not self.outcome_id:
            raise ValueError("outcome_id cannot be empty")
        if not self.signal_id:
            raise ValueError("signal_id cannot be empty")
        if not self.symbol:
            raise ValueError("symbol cannot be empty")
