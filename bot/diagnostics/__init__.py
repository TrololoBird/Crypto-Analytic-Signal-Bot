"""Diagnostics — signals funnel, runtime health, config audit, quality."""

from __future__ import annotations

import importlib
from typing import Any

from .signals import SignalDiagnostics, set_global_diagnostics

_LAZY = {
    "AlertManager": (".runtime", "AlertManager"),
    "AlertSeverity": (".runtime", "AlertSeverity"),
    "BotMetrics": (".runtime", "BotMetrics"),
    "HealthChecker": (".runtime", "HealthChecker"),
    "HealthStatus": (".runtime", "HealthStatus"),
    "MetricsExporter": (".runtime", "MetricsExporter"),
}

__all__ = ["SignalDiagnostics", "set_global_diagnostics", *sorted(_LAZY)]


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(spec[0], __name__)
    return getattr(module, spec[1])
