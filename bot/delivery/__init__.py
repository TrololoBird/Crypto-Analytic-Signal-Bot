"""Signal delivery path — contract, confluence, filters, Telegram.

Heavy submodules load lazily so ``domain.schemas`` can import ``contract`` without cycles.
"""

from __future__ import annotations

import importlib
from typing import Any

from .contract import (
    DEFAULT_SCALE_WEIGHTS,
    DEFAULT_TARGET_RR,
    SignalContractIssue,
    TradePlan,
    build_trade_plan,
    default_ttl_bars,
    normalize_scale_weights,
    signal_contract_row,
    valid_until_from,
    validate_signal_contract,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ConfluenceEngine": (".confluence", "ConfluenceEngine"),
    "DeliveredSignal": (".deliver", "DeliveredSignal"),
    "DeliveryResult": (".telegram", "DeliveryResult"),
    "MessageBroadcaster": (".telegram", "MessageBroadcaster"),
    "SignalBroadcaster": (".deliver", "SignalBroadcaster"),
    "SignalDelivery": (".deliver", "SignalDelivery"),
    "TierDecision": (".tiers", "TierDecision"),
    "apply_global_filters": (".filters", "apply_global_filters"),
    "build_message_broadcaster": (".telegram", "build_message_broadcaster"),
    "classify_tier": (".tiers", "classify_tier"),
    "TradePlanBuilder": (".trade_plan", "TradePlanBuilder"),
    "AlertCoordinator": (".watch", "AlertCoordinator"),
    "WatchCandidate": (".watch", "WatchCandidate"),
    "format_analytics_companion": (".deliver", "format_analytics_companion"),
    "format_analytics_companion_message": (".formatting", "format_analytics_companion_message"),
    "format_signal_message": (".formatting", "format_signal_message"),
    "format_signal_text": (".deliver", "format_signal_text"),
    "format_tracked_signal_message": (".formatting", "format_tracked_signal_message"),
    "format_tracked_signal_text": (".deliver", "format_tracked_signal_text"),
    "format_tracking_event_message": (".formatting", "format_tracking_event_message"),
    "format_tracking_event_text": (".deliver", "format_tracking_event_text"),
    "tradingview_chart_url": (".deliver", "tradingview_chart_url"),
}

__all__ = [
    "DEFAULT_SCALE_WEIGHTS",
    "DEFAULT_TARGET_RR",
    "AlertCoordinator",
    "ConfluenceEngine",
    "DeliveredSignal",
    "DeliveryResult",
    "MessageBroadcaster",
    "SignalBroadcaster",
    "SignalContractIssue",
    "SignalDelivery",
    "TierDecision",
    "TradePlan",
    "TradePlanBuilder",
    "WatchCandidate",
    "apply_global_filters",
    "build_message_broadcaster",
    "build_trade_plan",
    "classify_tier",
    "default_ttl_bars",
    "format_analytics_companion",
    "format_analytics_companion_message",
    "format_signal_message",
    "format_signal_text",
    "format_tracked_signal_message",
    "format_tracked_signal_text",
    "format_tracking_event_message",
    "format_tracking_event_text",
    "normalize_scale_weights",
    "signal_contract_row",
    "tradingview_chart_url",
    "valid_until_from",
    "validate_signal_contract",
]


def __getattr__(name: str) -> Any:
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module = importlib.import_module(spec[0], __name__)
    return getattr(module, spec[1])
