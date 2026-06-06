"""Startup doctor: fail-fast checks before the bot enters the hot path."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from bot.migrations import MIGRATIONS
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.domain.config import BotSettings

LOG = logging.getLogger("bot.diagnostics.startup_doctor")


class StartupDoctorError(RuntimeError):
    """Critical startup validation failure."""


def _expected_schema_version() -> int:
    return max(version for version, _, _ in MIGRATIONS)


def _check_writable_dirs(settings: BotSettings) -> list[str]:
    issues: list[str] = []
    candidates = (
        settings.logs_dir,
        settings.telemetry_dir,
        settings.db_path.parent,
        settings.data_dir,
    )
    seen: set[str] = set()
    for raw in candidates:
        path = Path(raw)
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            issues.append(f"dir_not_creatable:{path}:{exc}")
            continue
        if not os.access(path, os.W_OK):
            issues.append(f"dir_not_writable:{path}")
    return issues


def _check_schema_version(settings: BotSettings) -> list[str]:
    db_path = Path(settings.db_path)
    if not db_path.exists():
        return []
    expected = _expected_schema_version()
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()
    except sqlite3.Error as exc:
        return [f"schema_version_unreadable:{exc}"]
    current = int(row[0]) if row and row[0] is not None else 0
    if current < expected:
        return [f"schema_version_stale:current={current} expected>={expected}"]
    return []


def _check_enabled_strategies(settings: BotSettings, registered_setup_ids: set[str]) -> list[str]:
    enabled = set(settings.setups.enabled_setup_ids())
    missing = sorted(enabled - registered_setup_ids)
    if not missing:
        return []
    return [f"enabled_strategies_unregistered:{','.join(missing)}"]


def _check_telegram(settings: BotSettings) -> list[str]:
    if settings.notifiers.provider != "telegram":
        return []
    try:
        from bot.secrets import load_secrets

        secrets = load_secrets()
    except DEFENSIVE_EXC as exc:
        return [f"telegram_credentials_error:{exc}"]
    issues: list[str] = []
    if not str(secrets.tg_token or "").strip():
        issues.append("telegram_bot_token_missing")
    if not str(secrets.target_chat_id or "").strip():
        issues.append("telegram_chat_id_missing")
    return issues


def _check_scoring_weights(settings: BotSettings) -> list[str]:
    scoring = getattr(settings, "scoring", None)
    if scoring is None or not bool(getattr(scoring, "enabled", True)):
        return []
    weights = (
        float(getattr(scoring, "weight_mtf_alignment", 0.0)),
        float(getattr(scoring, "weight_volume_quality", 0.0)),
        float(getattr(scoring, "weight_structure_clarity", 0.0)),
        float(getattr(scoring, "weight_risk_reward", 0.0)),
        float(getattr(scoring, "weight_crowd_position", 0.0)),
        float(getattr(scoring, "weight_oi_momentum", 0.0)),
        float(getattr(scoring, "weight_liquidation_proximity", 0.0)),
        float(getattr(scoring, "weight_session_killzone", 0.0)),
    )
    total = sum(weights)
    if total <= 0.0:
        return ["scoring_weights_zero_total"]
    if abs(total - 1.0) > 0.05:
        return [f"scoring_weights_off_unit:total={total:.4f}"]
    return []


def run_startup_doctor(
    settings: BotSettings,
    *,
    registered_setup_ids: set[str],
    fail_fast: bool | None = None,
) -> list[str]:
    """Return critical issue codes; optionally abort startup."""
    issues: list[str] = []
    issues.extend(_check_writable_dirs(settings))
    issues.extend(_check_schema_version(settings))
    issues.extend(_check_enabled_strategies(settings, registered_setup_ids))
    issues.extend(_check_telegram(settings))
    issues.extend(_check_scoring_weights(settings))

    if issues:
        LOG.error("DOCTOR FAILED | issues=%s", issues)
        should_fail = (
            bool(fail_fast)
            if fail_fast is not None
            else bool(getattr(settings.runtime, "doctor_fail_fast", True))
        )
        if should_fail:
            raise StartupDoctorError("; ".join(issues))
    else:
        LOG.info(
            "DOCTOR OK | config=%s strategies=%d schema>=%d",
            settings.config_path,
            len(registered_setup_ids),
            _expected_schema_version(),
        )
    return issues
