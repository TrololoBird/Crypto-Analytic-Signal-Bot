from __future__ import annotations

import asyncio
from typing import Any


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
}

_DATA_ERROR_NAMES = {
    "indexerror",
    "zerodivisionerror",
}


def classify_runtime_error(exc: BaseException) -> str:
    """Return a coarse runtime error class for live-path telemetry."""
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
