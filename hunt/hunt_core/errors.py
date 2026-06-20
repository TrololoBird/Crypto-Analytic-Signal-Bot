from __future__ import annotations


import asyncio
import math
from typing import Any

# BLE001-safe tuple for log-and-continue / degrade paths (not CancelledError - BaseException).
DEFENSIVE_EXC: tuple[type[BaseException], ...] = (
    OSError,
    ConnectionError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    asyncio.TimeoutError,
)


def defensive_exc_types(*extra: type[BaseException]) -> tuple[type[BaseException], ...]:
    """Flatten DEFENSIVE_EXC with extra types for ``except`` clauses (never nest the tuple)."""
    return DEFENSIVE_EXC + extra


_NETWORK_ERROR_NAMES = {
    "aiohttperror",
    "clienterror",
    "clientconnectorerror",
    "clientpayloaderror",
    "socketerror",
    "timeout",
    "timeouterror",
    "connectionerror",
    "oserror",
}

_SCHEMA_ERROR_NAMES = {
    "msgspecerror",
    "validationerror",
    "typeerror",
    "keyerror",
    "attributeerror",
    "columnnotfounderror",  # polars missing column
    "invalidoperationerror",  # polars schema mismatch
    "schemamismatcherror",
}

_DATA_ERROR_NAMES = {
    "indexerror",
    "zerodivisionerror",
}


def classify_runtime_error(exc: BaseException) -> str:
    """Return a coarse runtime error class for live-path telemetry."""
    try:
        import ccxt

        if isinstance(exc, ccxt.DDoSProtection):
            return "ip_ban" if "418" in str(exc) else "rate_limit"
        if isinstance(exc, (ccxt.RateLimitExceeded,)):
            return "rate_limit"
        if isinstance(exc, ccxt.ExchangeNotAvailable):
            text = str(exc).lower()
            if "418" in text or "ban" in text:
                return "ip_ban"
            if "429" in text or "rate limit" in text:
                return "rate_limit"
            return "network"
        if isinstance(exc, ccxt.NetworkError):
            return "network"
    except Exception:
        pass
    name = exc.__class__.__name__.lower()

    if isinstance(exc, asyncio.TimeoutError) or name in _NETWORK_ERROR_NAMES:
        return "network"
    if name in _SCHEMA_ERROR_NAMES:
        return "schema"
    if name in _DATA_ERROR_NAMES:
        return "data"
    return "bug"


def build_runtime_error_payload(
    *,
    component: str,
    exc: BaseException,
    setup_id: str | None = None,
    symbol: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "component": component,
        "error_class": classify_runtime_error(exc),
        "exception_type": exc.__class__.__name__,
        "error": str(exc),
    }
    if setup_id:
        payload["setup_id"] = setup_id
    if symbol:
        payload["symbol"] = symbol
    if extra:
        payload.update(extra)
    return payload


class SignalDataMissing(Exception):
    """Required signal-path field absent or non-finite."""

    def __init__(self, field: str, *, detail: str = "") -> None:
        self.field = field
        self.detail = detail
        msg = f"signal_data_missing:{field}"
        if detail:
            msg = f"{msg}:{detail}"
        super().__init__(msg)


def require_finite_float(value: Any, field: str) -> float:
    if value is None:
        raise SignalDataMissing(field)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise SignalDataMissing(field, detail="not_numeric") from exc
    if not math.isfinite(numeric):
        raise SignalDataMissing(field, detail="non_finite")
    return numeric


def optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def require_mark_price(
    price: Any,
    market: dict[str, Any] | None,
    *,
    field: str = "price",
) -> float:
    mkt = market or {}
    for mark_key in ("mark_price", "markPrice", "live_mark_price"):
        val = optional_finite_float(mkt.get(mark_key))
        if val is not None and val > 0:
            return val
    for candidate in (price, mkt.get("last_price")):
        val = optional_finite_float(candidate)
        if val is not None and val > 0:
            return val
    raise SignalDataMissing(field)


def require_level(value: Any, field: str) -> float:
    val = optional_finite_float(value)
    if val is None or val <= 0:
        raise SignalDataMissing(field)
    return val


def finite_float_or_none(value: object) -> float | None:
    """Return a finite float or None — never substitute 0 for missing market data."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return default
        return numeric if math.isfinite(numeric) else default
    return default


def as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def row_float(row: object, key: str, default: float = 0.0) -> float:
    if not isinstance(row, dict):
        return default
    return as_float(row.get(key), default=default)
