"""Diagnostics - signals funnel, runtime health, config audit, quality."""

from __future__ import annotations

import importlib
from typing import Any

from .signals import SignalDiagnostics, set_global_diagnostics

_LAZY = {
    "AlertManager": (".runtime_ops", "AlertManager"),
    "AlertSeverity": (".runtime_ops", "AlertSeverity"),
    "BotMetrics": (".runtime_ops", "BotMetrics"),
    "HealthChecker": (".runtime_ops", "HealthChecker"),
    "HealthStatus": (".runtime_ops", "HealthStatus"),
    "MetricsExporter": (".runtime_ops", "MetricsExporter"),
}

__all__ = [
    "AlertManager",
    "AlertSeverity",
    "BotMetrics",
    "HealthChecker",
    "HealthStatus",
    "MetricsExporter",
    "SignalDiagnostics",
    "set_global_diagnostics",
]


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module = importlib.import_module(spec[0], __name__)
    return getattr(module, spec[1])
