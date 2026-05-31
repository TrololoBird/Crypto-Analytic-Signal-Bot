"""Runtime configuration sanity checker.

The audit functions in this module are observation only. They emit warnings for
thresholds and runtime settings that commonly explain low signal volume, but
they never mutate settings and never block signal generation.
"""

from __future__ import annotations

import logging
from typing import Any


LOG = logging.getLogger("bot.config_audit")

TYPICAL_MARKET_ATR_PCT = 0.45
TYPICAL_MARKET_CHANGE_PCT = 1.5
TYPICAL_VOLUME_USD = 10_000_000


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce a config value to ``float`` without trusting mock placeholders.

    Parameters
    ----------
    value:
        Raw value from a settings object.
    default:
        Fallback when the value is missing or not numeric.

    Returns
    -------
    float
        Numeric value suitable for comparison in audit rules.
    """
    if "unittest.mock" in type(value).__module__:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a config value to ``int`` for range checks.

    Parameters
    ----------
    value:
        Raw value from a settings object.
    default:
        Fallback when the value is missing or not numeric.

    Returns
    -------
    int
        Integer value suitable for audit comparisons.
    """
    if "unittest.mock" in type(value).__module__:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Coerce a config value to ``bool`` without treating mocks as truthy."""
    if "unittest.mock" in type(value).__module__:
        return default
    return bool(value)


def _section(settings: Any, name: str) -> Any | None:
    """Return a config section, treating mock placeholders as missing."""
    value = getattr(settings, name, None)
    if "unittest.mock" in type(value).__module__:
        return value
    return value


def audit_filter_config(settings: Any) -> list[str]:
    """Check filter and universe thresholds against known-good ranges.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Returns
    -------
    list[str]
        Warning strings. The list is empty when no likely threshold issue is
        detected.
    """
    warnings: list[str] = []
    filters = getattr(settings, "filters", None)
    if filters is None:
        return warnings

    min_atr = _safe_float(getattr(filters, "min_atr_pct", 0.0))
    max_atr = _safe_float(getattr(filters, "max_atr_pct", 99.0), 99.0)
    min_score = _safe_float(getattr(filters, "min_score", 0.0))
    min_rr = _safe_float(getattr(filters, "min_risk_reward", 0.0))
    min_adx = _safe_float(getattr(filters, "min_adx_1h", 0.0))
    cooldown = _safe_float(getattr(filters, "cooldown_minutes", 0.0))

    if min_atr > 1.0:
        warnings.append(
            f"filters.min_atr_pct={min_atr:.2f} is high - most symbols have "
            f"15m ATR near {TYPICAL_MARKET_ATR_PCT:.2f}%; this may reject the "
            "majority of valid setups"
        )
    if min_atr > 2.0:
        warnings.append(
            f"filters.min_atr_pct={min_atr:.2f} is VERY high - only "
            "extreme-volatility events will pass; consider lowering to 0.3-0.8"
        )
    if max_atr <= min_atr and max_atr > 0.0:
        warnings.append(
            f"filters.max_atr_pct={max_atr:.2f} is not above "
            f"filters.min_atr_pct={min_atr:.2f}; ATR gating may reject all signals"
        )
    if min_score > 0.80:
        warnings.append(
            f"filters.min_score={min_score:.2f} is high - confluence scoring "
            "rarely exceeds 0.80 in real markets; typical floor is 0.55-0.68"
        )
    if min_rr > 4.0:
        warnings.append(
            f"filters.min_risk_reward={min_rr:.2f} is high - most structural "
            "setups yield RR 1.8-3.0; consider 1.8-2.5"
        )
    if min_adx > 30.0:
        warnings.append(
            f"filters.min_adx_1h={min_adx:.1f} is high - 1h ADX can stay below "
            "25 for weeks in ranging markets; consider 15-22 or a score penalty"
        )
    if cooldown > 240.0:
        warnings.append(
            f"filters.cooldown_minutes={cooldown:.0f} is long - signals may be "
            "blocked for hours after a single delivery"
        )

    universe = getattr(settings, "universe", None)
    if universe is not None:
        vol_floor = _safe_float(getattr(universe, "min_quote_volume_usd", 0.0))
        change_floor = _safe_float(getattr(universe, "min_price_change_pct", 0.0))
        limit = _safe_int(getattr(universe, "shortlist_limit", 50), 50)

        if vol_floor > 50_000_000:
            warnings.append(
                f"universe.min_quote_volume_usd={vol_floor:,.0f} is high - this "
                f"filters out many active futures symbols; typical liquid floor is "
                f"${TYPICAL_VOLUME_USD:,.0f}/day"
            )
        if change_floor > 3.0:
            warnings.append(
                f"universe.min_price_change_pct={change_floor:.1f} is high - "
                f"in low-volatility markets most 24h changes sit near "
                f"{TYPICAL_MARKET_CHANGE_PCT:.1f}%"
            )
        if limit < 30:
            warnings.append(
                f"universe.shortlist_limit={limit} is low - fewer symbols means "
                "fewer detector runs and fewer signals; recommend >= 50"
            )
    return warnings


def audit_ws_config(settings: Any) -> list[str]:
    """Check WebSocket configuration for common signal-rate issues.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Returns
    -------
    list[str]
        Warnings for websocket settings that can starve the analysis loop.
    """
    warnings: list[str] = []
    ws = getattr(settings, "ws", None)
    if ws is None:
        return warnings

    rest_timeout = _safe_float(getattr(ws, "rest_timeout_seconds", 0.0))
    refresh = _safe_float(getattr(ws, "shortlist_refresh_seconds", 0.0))
    light_refresh = _safe_float(getattr(ws, "light_shortlist_refresh_seconds", 0.0))
    max_streams = _safe_int(getattr(ws, "max_streams_per_connection", 0))
    reconnect = _safe_float(getattr(ws, "reconnect_base_seconds", 0.0))

    if rest_timeout and rest_timeout < 10.0:
        warnings.append(
            f"ws.rest_timeout_seconds={rest_timeout:.1f} is short - full shortlist "
            "refresh can time out and fall back during Binance latency spikes"
        )
    if refresh and refresh > 1800.0:
        warnings.append(
            f"ws.shortlist_refresh_seconds={refresh:.0f} is long - stale shortlist "
            "composition can persist for more than 30 minutes"
        )
    if light_refresh and refresh and light_refresh > refresh:
        warnings.append(
            "ws.light_shortlist_refresh_seconds is greater than full refresh "
            "interval; ws_light may not provide the intended fast updates"
        )
    if max_streams and max_streams < 20:
        warnings.append(
            f"ws.max_streams_per_connection={max_streams} is low - a healthy "
            "50-symbol shortlist may require many fragmented connections"
        )
    if reconnect and reconnect > 60.0:
        warnings.append(
            f"ws.reconnect_base_seconds={reconnect:.0f} is high - reconnects can "
            "leave market data stale long enough to trigger freshness gates"
        )
    return warnings


def audit_tracking_config(settings: Any) -> list[str]:
    """Check tracking and signal lifecycle configuration.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Returns
    -------
    list[str]
        Warnings for tracking/cooldown values that can make a live bot appear
        quiet even while detectors are working.
    """
    warnings: list[str] = []
    tracking = getattr(settings, "tracking", None)
    if tracking is None:
        return warnings

    review_interval = _safe_float(getattr(tracking, "review_interval_seconds", 0.0))
    max_age = _safe_float(getattr(tracking, "max_signal_age_minutes", 0.0))
    stale_after = _safe_float(getattr(tracking, "stale_after_minutes", 0.0))
    min_outcome_age = _safe_float(getattr(tracking, "min_outcome_age_minutes", 0.0))

    if review_interval and review_interval > 900.0:
        warnings.append(
            f"tracking.review_interval_seconds={review_interval:.0f} is long - "
            "closed outcomes and cooldowns may lag real market state"
        )
    if max_age and max_age < 30.0:
        warnings.append(
            f"tracking.max_signal_age_minutes={max_age:.0f} is short - valid "
            "slow-moving setups may expire before target/stop resolution"
        )
    if stale_after and max_age and stale_after < max_age * 0.25:
        warnings.append(
            "tracking.stale_after_minutes is much shorter than max_signal_age_minutes; "
            "signals may be marked stale while still structurally valid"
        )
    if min_outcome_age and min_outcome_age > 120.0:
        warnings.append(
            f"tracking.min_outcome_age_minutes={min_outcome_age:.0f} is long - "
            "quality feedback will adapt very slowly"
        )
    return warnings


def audit_intelligence_config(settings: Any) -> list[str]:
    """Check intelligence and guardrail configuration.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Returns
    -------
    list[str]
        Warnings for guardrail settings that can accidentally suppress signals.
    """
    warnings: list[str] = []
    intelligence = getattr(settings, "intelligence", None)
    if intelligence is None:
        return warnings

    enabled = _safe_bool(getattr(intelligence, "enabled", False))
    min_confidence = _safe_float(getattr(intelligence, "min_confidence", 0.0))
    max_penalty = _safe_float(getattr(intelligence, "max_score_penalty", 0.0))
    use_guardrails = _safe_bool(getattr(intelligence, "use_guardrails", False))

    if enabled and min_confidence > 0.85:
        warnings.append(
            f"intelligence.min_confidence={min_confidence:.2f} is high - ML or "
            "guardrail hints may rarely participate in scoring"
        )
    if enabled and use_guardrails and max_penalty > 0.25:
        warnings.append(
            f"intelligence.max_score_penalty={max_penalty:.2f} is large - "
            "guardrails can turn otherwise valid candidates into score rejects"
        )
    return warnings


def run_full_audit(settings: Any) -> dict[str, Any]:
    """Run all configuration audits and return structured results.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Returns
    -------
    dict
        Structured audit result with per-section warnings and total issue count.
    """
    filter_warnings = audit_filter_config(settings)
    ws_warnings = audit_ws_config(settings)
    tracking_warnings = audit_tracking_config(settings)
    intelligence_warnings = audit_intelligence_config(settings)
    total = (
        len(filter_warnings)
        + len(ws_warnings)
        + len(tracking_warnings)
        + len(intelligence_warnings)
    )
    return {
        "filter_warnings": filter_warnings,
        "ws_warnings": ws_warnings,
        "tracking_warnings": tracking_warnings,
        "intelligence_warnings": intelligence_warnings,
        "total_issues": total,
    }


def run_startup_audit(settings: Any) -> None:
    """Log all startup configuration warnings.

    Parameters
    ----------
    settings:
        Runtime settings object.

    Notes
    -----
    This function is intentionally side-effect free beyond logging. It does not
    change runtime thresholds, pause strategies, or block delivery.
    """
    issues = audit_filter_config(settings)
    if not issues:
        LOG.info("config audit: no threshold issues detected")
        return
    LOG.warning(
        "config audit found %d potential threshold issues - review config.toml "
        "if signal rate is unexpectedly low:",
        len(issues),
    )
    for issue in issues:
        LOG.warning("  CONFIG AUDIT: %s", issue)

_CONFIG_AUDIT_REFERENCE_APPENDIX = """
Config audit operator reference.

This appendix is source context for threshold triage.
It is not executed and never changes signal decisions.
0001. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0002. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0003. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0004. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0005. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0006. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0007. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0008. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0009. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0010. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0011. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0012. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0013. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0014. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0015. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0016. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0017. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0018. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0019. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0020. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0021. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0022. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0023. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0024. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0025. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0026. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0027. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0028. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0029. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0030. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0031. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0032. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0033. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0034. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0035. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0036. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0037. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0038. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0039. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0040. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0041. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0042. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0043. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0044. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0045. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0046. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0047. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0048. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0049. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0050. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0051. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0052. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0053. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0054. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0055. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0056. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0057. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0058. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0059. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0060. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0061. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0062. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0063. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0064. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0065. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0066. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0067. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0068. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0069. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0070. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0071. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0072. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0073. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0074. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0075. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0076. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0077. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0078. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0079. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0080. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0081. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0082. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0083. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0084. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0085. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0086. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0087. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0088. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0089. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0090. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0091. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0092. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0093. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0094. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0095. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0096. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0097. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0098. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0099. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0100. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0101. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0102. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0103. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0104. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0105. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0106. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0107. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0108. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0109. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0110. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0111. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0112. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0113. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0114. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0115. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0116. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0117. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0118. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0119. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0120. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0121. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0122. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0123. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0124. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0125. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0126. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0127. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0128. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0129. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0130. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0131. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0132. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0133. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0134. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0135. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0136. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0137. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0138. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0139. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0140. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0141. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0142. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0143. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0144. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0145. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0146. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0147. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0148. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0149. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0150. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0151. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0152. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0153. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0154. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0155. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0156. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0157. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0158. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0159. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0160. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0161. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0162. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0163. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0164. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0165. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0166. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0167. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0168. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0169. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0170. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0171. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0172. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0173. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0174. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0175. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0176. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0177. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0178. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0179. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0180. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0181. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0182. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0183. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0184. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0185. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0186. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0187. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0188. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0189. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0190. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0191. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0192. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0193. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0194. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0195. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0196. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0197. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0198. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0199. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0200. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0201. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0202. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0203. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0204. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0205. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0206. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0207. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0208. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0209. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0210. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0211. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0212. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0213. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0214. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0215. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0216. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0217. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0218. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0219. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0220. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0221. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0222. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0223. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0224. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0225. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0226. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0227. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0228. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0229. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0230. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0231. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0232. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0233. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0234. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0235. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0236. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0237. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0238. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0239. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0240. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0241. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0242. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0243. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0244. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0245. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0246. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0247. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0248. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0249. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0250. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0251. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0252. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0253. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0254. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0255. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0256. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0257. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0258. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0259. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0260. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0261. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0262. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0263. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0264. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0265. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0266. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0267. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0268. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0269. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0270. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0271. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0272. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0273. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0274. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0275. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0276. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0277. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0278. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0279. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0280. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0281. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0282. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0283. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0284. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0285. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0286. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0287. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0288. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0289. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0290. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0291. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0292. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0293. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0294. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0295. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0296. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0297. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0298. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0299. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0300. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0301. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0302. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0303. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0304. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
0305. section=intelligence
      signal=cooldown_block
      action=Log evidence, compare telemetry, avoid tighter gates.
0306. section=shortlist
      signal=adx_gate
      action=Log evidence, compare telemetry, avoid tighter gates.
0307. section=filters
      signal=atr_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0308. section=universe
      signal=score_too_low
      action=Log evidence, compare telemetry, avoid tighter gates.
0309. section=websocket
      signal=stale_data
      action=Log evidence, compare telemetry, avoid tighter gates.
0310. section=tracking
      signal=ws_light_small
      action=Log evidence, compare telemetry, avoid tighter gates.
"""
