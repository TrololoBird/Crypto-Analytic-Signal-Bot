"""Wave E8 / Agent J: ops webhook auto-enable, monitor lock, db status, config warnings."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.domain.config import NotifierWebhookConfig
from bot.ops.pid_utils import acquire_pid_lock, read_pid_file, release_pid_lock
from bot.persistence.db_status import (
    DbStatusSummary,
    collect_db_status_from_conn,
    format_db_status_html,
)
from scripts.validate_config import _ops_webhook_auto_enable_warning


def test_notifier_webhook_auto_enables_ops_alerts_when_url_set() -> None:
    cfg = NotifierWebhookConfig(
        enabled=False,
        webhook_url="https://hooks.example.com/ops",
        ops_alerts_enabled=False,
    )
    assert cfg.ops_alerts_enabled is True


def test_notifier_webhook_keeps_ops_disabled_without_url() -> None:
    cfg = NotifierWebhookConfig(enabled=False, webhook_url=None, ops_alerts_enabled=False)
    assert cfg.ops_alerts_enabled is False


def test_ops_webhook_auto_enable_warning_from_toml(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[bot.notifiers.webhook]
webhook_url = "https://hooks.example.com/ops"
ops_alerts_enabled = false
""".strip(),
        encoding="utf-8",
    )
    msg = _ops_webhook_auto_enable_warning(config)
    assert msg is not None
    assert "auto-enabling" in msg


def test_ops_webhook_no_warning_when_explicitly_enabled(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[bot.notifiers.webhook]
webhook_url = "https://hooks.example.com/ops"
ops_alerts_enabled = true
""".strip(),
        encoding="utf-8",
    )
    assert _ops_webhook_auto_enable_warning(config) is None


def test_monitor_lock_exits_when_holder_alive(tmp_path: Path) -> None:
    lock = tmp_path / "monitor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(SystemExit, match="another process"):
        acquire_pid_lock(lock, owner_pid=os.getpid() + 999_999)


def test_monitor_lock_acquire_and_release(tmp_path: Path) -> None:
    lock = tmp_path / "monitor.lock"
    acquire_pid_lock(lock)
    assert read_pid_file(lock) == os.getpid()
    release_pid_lock(lock)
    assert not lock.exists()


def test_format_db_status_html_includes_migration_and_counts() -> None:
    summary = DbStatusSummary(
        migration_version=4,
        migrations=[(4, "relabel_legacy_setup_invalidated", "2026-01-01")],
        signal_counts={"pending": 2, "active": 1},
    )
    text = format_db_status_html(summary)
    assert "v4" in text
    assert "pending" in text
    assert "active" in text


def test_makefile_graphify_update_skips_when_missing() -> None:
    repo = Path(__file__).resolve().parents[1]
    makefile = (repo / "Makefile").read_text(encoding="utf-8")
    assert "graphify-update:" in makefile
    assert "graphify not installed" in makefile


def test_run_monitored_bot_uses_monitor_lock_path() -> None:
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "scripts" / "run_monitored_bot.py").read_text(encoding="utf-8")
    assert "data/bot/monitor.lock" in source
    assert "acquire_pid_lock" in source


@pytest.mark.asyncio
async def test_collect_db_status_from_conn() -> None:

    class _Cursor:
        async def fetchall(self) -> list[tuple[str, int]]:
            return [("pending", 3), ("active", 1)]

        async def fetchone(self) -> tuple[int] | None:
            return None

    class _Conn:
        async def execute(self, sql: str) -> _Cursor:
            if "schema_version" in sql:
                msg = "fetch_schema_version_rows should be patched"
                raise AssertionError(msg)
            return _Cursor()

    conn = _Conn()
    with patch(
        "bot.persistence.db_status.fetch_schema_version_rows",
        return_value=[(4, "test_migration", "2026-01-01")],
    ):
        summary = await collect_db_status_from_conn(conn)  # type: ignore[arg-type]
    assert summary.migration_version == 4
    assert summary.signal_counts == {"pending": 3, "active": 1}


def test_telegram_status_includes_db_block() -> None:
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "bot" / "runtime" / "telegram_operator.py").read_text(encoding="utf-8")
    assert "_format_db_status_text" in source
    assert "format_db_status_html" in source
