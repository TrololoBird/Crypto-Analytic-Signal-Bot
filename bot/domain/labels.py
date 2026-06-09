"""Canonical RU/EN labels for rejects, outcomes, and internal tracking events."""

from __future__ import annotations

from bot.domain.mtf import normalize_mtf_reject_reason

# Delivery-time reject reasons (never reached channel).
REJECT_REASON_RU: dict[str, str] = {
    "score_too_low": "низкая оценка",
    "shortlist_not_routed": "не в shortlist",
    "confirmation_failed": "нет подтверждения",
    "tracking_blocked": "уже отслеживается",
    "hard_confluence_gate": "слабый confluence",
    "mark_price_deviation": "расхождение mark price",
    "family_precheck_failed": "семейный фильтр",
    "entry_staleness": "цена ушла от entry (>ATR)",
    "limit_late_entry_chase": "цена ушла от зоны (limit)",
    "limit_publish_rejected": "план недействителен при публикации",
    "limit_setup_invalidated": "план недействителен при публикации",  # legacy alias
    "regime_bear_long_blocked": "long в bear режиме",
    "regime_long_blocked": "long заблокирован режимом",
    "btc_downtrend_long_blocked": "long против BTC↓",
    "htf_conflict": "конфликт с 1h/4h трендом",
    "htf_reversal_conflict": "оба HTF против разворотного сигнала",
    "stale_15m": "устаревшие 15m данные",
    "stale_1h": "устаревшие 1h данные",
    "stale_4h": "устаревшие 4h данные",
    "atr_too_low": "низкая волатильность (ATR)",
    "atr_too_high": "высокая волатильность (ATR)",
    "spread_too_wide": "широкий спред",
    "risk_reward_too_low": "низкий R:R",
    "stop_too_tight": "слишком узкий стоп",
    "adx_penalty_score_too_low": "низкая оценка после ADX",
    "filter_pipeline_crash": "сбой фильтра",
    "r_class_action_blocked": "R-класс только WATCH",
    "5m_opposes_long": "5m против long",
    "5m_opposes_short": "5m против short",
    "benchmark_context_conflict": "конфликт бенчмарка",
    "macro_risk_off_long": "macro risk-off для long",
    "portfolio_family_direction_cap": "лимит семейства",
    "portfolio_direction_regime_cap": "лимит портфеля",
    "action_session_cap_reached": "лимит ACTION за сессию",
    "action_session_cap_downgrade": "ACTION→WATCH (лимит сессии)",
    "action_cap_reached": "лимит ACTION за цикл",
    "watch_cap_reached": "лимит WATCH за цикл",
    "runtime.strategy_schedule_inactive": "вне расписания",
    "runtime.strategy_lane_excluded": "исключён lane cap",
    "asset_fit.shortlist_not_routed": "не в shortlist",
    # Catalog guards (bot/domain/catalog_guards.py)
    "catalog_min_volume": "низкий объём (catalog)",
    "catalog_min_adx_1h": "низкий ADX 1h (catalog)",
    "catalog_htf_bias_conflict": "конфликт HTF bias (catalog)",
    # Delivery filters / MTF
    "spread_unavailable": "спред недоступен",
    "atr_unavailable": "ATR недоступен",
    "htf_frames_missing": "нет HTF фреймов",
    "regime_not_suitable": "режим не подходит",
    "regime_bull_reversal_short_blocked": "шорт-разворот в bull режиме",
    "regime_bull_short_blocked": "short в bull режиме",
    "btc_uptrend_short_blocked": "short против BTC↑",
    "supertrend_up_reversal_short_blocked": "шорт-разворот при supertrend↑",
    "regime_bear_reversal_long_blocked": "long-разворот в bear режиме",
    "reversal_short_trending_regime_blocked": "шорт-разворот в тренде",
    "reversal_long_trending_regime_blocked": "long-разворот в тренде",
    "volume_climax_trend_regime_blocked": "volume climax в тренде",
    "volume_climax_continuation_bar": "climax: продолжение тренда",
    "volume_climax_strong_trend_adx": "climax: сильный ADX тренд",
    "cvd_trend_regime_blocked": "CVD-разворот в тренде",
    "cvd_divergence_stale": "устаревшая CVD-дивергенция",
    "order_block_no_bos_after_ob": "OB без BOS после импульса",
    "order_block_body_too_small": "OB: тело свечи < 0.5×ATR",
    "liquidity_sweep_wick_too_shallow": "sweep: хвост < 0.2×ATR",
    "structure_break_retest_1h_conflict": "BOS-retest против структуры 1h",
    "btc_uptrend_alt_short_blocked": "alt short против BTC↑",
    # Data-plane readiness and strategy capability gates
    "data.insufficient_required_history": "недостаточно истории баров",
    "data.mark_price_missing": "нет mark price",
    "data.spread_missing": "нет спреда",
    "data.orderbook_columns_missing": "нет orderbook колонок",
    "data.derivatives_context_missing": "нет derivatives context",
    "data.insufficient_input": "недостаточно входных данных",
    "data.work_1h_missing": "нет 1h данных",
    "data.work_1h_insufficient_history": "мало истории 1h",
    "data.oi_context_missing": "нет OI context",
    "data.funding_rate_missing": "нет funding rate",
    "data.required_features_missing": "нет обязательных фич",
    "data.required_enrichment_missing": "нет enrichment",
    "data.capability_not_ready": "данные не готовы",
    "data.orderbook_not_ready": "orderbook не готов",
    "data.positioning_not_ready": "positioning не готов",
    "data.orderflow_not_ready": "orderflow не готов",
    "data.ls_ratio_missing": "нет L/S ratio",
    "data.liquidation_score_missing": "нет liquidation score",
    "data.btc_context_missing": "нет BTC context",
    "data.base_asset_missing": "нет base asset",
    "data.altcoin_season_index_missing": "нет altcoin season index",
    "data.not_ready": "данные не готовы",
}

# Hard confluence gate legs (ADR-003) for dashboard analytics.
CONFLUENCE_LEG_LABEL_RU: dict[str, str] = {
    "trend": "тренд",
    "momentum": "импульс",
    "volume": "объём",
    "htf": "HTF",
    "microstructure": "микроструктура",
}

CONFLUENCE_LEG_KEYS: tuple[str, ...] = tuple(CONFLUENCE_LEG_LABEL_RU)

# Strategy confirmation profiles (catalog + hard confluence gate).
CONFIRMATION_PROFILE_LABEL_RU: dict[str, str] = {
    "trend_follow": "следование тренду",
    "breakout_acceptance": "принятие пробоя",
    "countertrend_exhaustion": "контртренд / истощение",
    "divergence_reversal": "разворот по дивергенции",
}

CONFIRMATION_PROFILE_KEYS: tuple[str, ...] = tuple(CONFIRMATION_PROFILE_LABEL_RU)

# Closed tracking / outcome labels shown in dashboard history.
RESULT_LABEL_RU: dict[str, str] = {
    "tp1_hit": "TP1 ✓",
    "tp2_hit": "TP2 ✓",
    "stop_loss": "Стоп",
    "breakeven_stop": "Безубыток",
    "expired": "Истёк",
    "expired_pending": "Истёк (ожидание)",
    "expired_active": "Истёк (активный)",
    "ambiguous_exit": "Неоднозначно",
    "smart_exit": "Умный выход",
    "emergency_exit": "Экстренный выход",
    "superseded": "Заменён",
    "setup_invalidated": "legacy: SL до лимита (устар.)",
    "legacy_setup_invalidated": "legacy: SL до лимита (устар.)",
}

# Channel lifecycle reply titles (subscribers).
TRACKING_EVENT_RU: dict[str, str] = {
    "activated": "В СДЕЛКЕ",
    "tp1_hit": "TP1",
    "tp2_hit": "TP2",
    "stop_loss": "СТОП",
    "breakeven_stop": "БЕЗУБЫТОК",
    "expired": "ЛИМИТ ИСТЁК",
    "ambiguous_exit": "НЕОДНОЗНАЧНО",
    "smart_exit": "ВЫХОД",
    "emergency_exit": "ВЫХОД",
    "superseded": "ЗАМЕНЁН",
}

# Internal-only lifecycle (operator/logs - not channel subscribers).
INTERNAL_TRACKING_EVENT_RU: dict[str, str] = {
    "entry_zone_touched": "ЗОНА",
    "entry_confirm_pending": "ОЖИДАНИЕ",
    "setup_invalidated": "legacy: SL до лимита",
    "unactivated_close": "ОТМЕНА ДО ВХОДА",
    "activation_blocked_supertrend_up_short": "блок: supertrend↑ шорт",
    "activation_blocked_supertrend_down_long": "блок: supertrend↓ лонг",
    "zone_invalidated_stop_breached": "зона пробита до входа",
    "activation_staleness": "устарело до активации",
    "pending_too_old": "истёк срок ожидания",
    "activation_score_decay": "score ниже порога",
    "activation_context_stale": "устаревший контекст",
    "activation_trend_regime_short_blocked": "блок: тренд шорт",
    "activation_trend_regime_long_blocked": "блок: тренд лонг",
}

# Normalize legacy telemetry / reject keys to canonical codes.
_REJECT_ALIASES: dict[str, str] = {
    "limit_setup_invalidated": "limit_publish_rejected",
    "hard_confluence_gate_failed": "hard_confluence_gate",
    "asset_fit.shortlist_not_routed": "shortlist_not_routed",
}


def normalize_reject_reason(key: str | None) -> str:
    """Map telemetry / legacy reject keys to canonical dashboard codes."""
    if not key:
        return ""
    normalized = str(key).strip().lower()
    aliased = _REJECT_ALIASES.get(normalized)
    if aliased:
        return aliased
    if normalized.startswith(("htf_reversal_conflict", "htf_conflict")):
        return normalize_mtf_reject_reason(normalized)
    base = normalized.split(":", 1)[0]
    return _REJECT_ALIASES.get(base, base)


def reject_reason_ru(key: str | None) -> str:
    if not key:
        return "фильтр"
    normalized = normalize_reject_reason(key)
    if normalized in REJECT_REASON_RU:
        return REJECT_REASON_RU[normalized]
    if normalized.startswith("data."):
        suffix = normalized.removeprefix("data.").replace("_", " ")
        return f"данные: {suffix}"
    return normalized.replace("_", " ")


def result_label_ru(result: str | None) -> str:
    if not result:
        return "-"
    return RESULT_LABEL_RU.get(str(result).lower(), str(result))


def tracking_event_ru(event_type: str | None) -> str:
    if not event_type:
        return "-"
    key = str(event_type).strip().lower()
    if key in TRACKING_EVENT_RU:
        return TRACKING_EVENT_RU[key]
    if key in INTERNAL_TRACKING_EVENT_RU:
        return INTERNAL_TRACKING_EVENT_RU[key]
    return key.replace("_", " ").upper()


def confluence_leg_label_ru(leg: str | None) -> str:
    if not leg:
        return "-"
    return CONFLUENCE_LEG_LABEL_RU.get(str(leg).strip().lower(), str(leg))


def confirmation_profile_label_ru(profile: str | None) -> str:
    if not profile:
        return "-"
    key = str(profile).strip().lower()
    return CONFIRMATION_PROFILE_LABEL_RU.get(key, key.replace("_", " "))


def confluence_profile_recommendation_ru(
    profile: str | None,
    *,
    top_leg: str | None,
    leg_count: int,
) -> str:
    """Operator hint when a confirmation profile repeatedly fails one confluence leg."""
    if not top_leg or leg_count <= 0:
        return ""
    profile_label = confirmation_profile_label_ru(profile)
    leg_label = confluence_leg_label_ru(top_leg)
    return (
        f"Профиль «{profile_label}»: чаще всего не проходит {leg_label} "
        f"({int(leg_count)} отказов) - проверьте пороги этого leg."
    )


def labels_payload() -> dict[str, dict[str, str]]:
    """JSON-serializable label maps for dashboard static JS."""
    return {
        "reject_reasons": dict(REJECT_REASON_RU),
        "confluence_legs": dict(CONFLUENCE_LEG_LABEL_RU),
        "confirmation_profiles": dict(CONFIRMATION_PROFILE_LABEL_RU),
        "results": dict(RESULT_LABEL_RU),
        "tracking_events": dict(TRACKING_EVENT_RU),
        "internal_tracking_events": dict(INTERNAL_TRACKING_EVENT_RU),
    }
