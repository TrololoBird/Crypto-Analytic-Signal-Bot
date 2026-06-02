"""Unit tests for public audit CSV + SHA256 ledger."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bot.domain.schemas import Signal
from bot.persistence.public_audit import PublicAuditLedger


def _sample_signal() -> Signal:
    return Signal(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=72.5,
        timeframe="15m",
        entry_low=99.5,
        entry_high=100.5,
        stop=95.0,
        take_profit_1=110.0,
        take_profit_2=115.0,
        risk_reward=2.0,
    )


def test_append_delivered_writes_csv_and_sha256(tmp_path: Path) -> None:
    ledger = PublicAuditLedger(tmp_path, enabled=True)
    ledger.append_delivered(_sample_signal(), tier="ACTION", message_id=42)

    csv_files = list(tmp_path.glob("signals_*.csv"))
    assert len(csv_files) == 1
    csv_path = csv_files[0]
    sha_path = tmp_path / f"{csv_path.stem}.sha256"
    assert sha_path.exists()

    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert digest in sha_path.read_text(encoding="utf-8")

    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("ts_utc,")
    assert "ema_bounce" in lines[1]
    assert "42" in lines[1]


def test_latest_manifest_lists_recent_files(tmp_path: Path) -> None:
    ledger = PublicAuditLedger(tmp_path, enabled=True)
    ledger.append_delivered(_sample_signal(), tier="WATCH", message_id=None)

    manifest = ledger.latest_manifest()
    assert manifest["enabled"] is True
    assert manifest["root"] == str(tmp_path)
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["rows"] == 1
    assert manifest["files"][0]["sha256"]


def test_disabled_ledger_skips_writes(tmp_path: Path) -> None:
    ledger = PublicAuditLedger(tmp_path, enabled=False)
    ledger.append_delivered(_sample_signal(), tier="ACTION", message_id=1)
    assert list(tmp_path.glob("signals_*.csv")) == []


def test_recent_action_signals_tracks_action_tier_only(tmp_path: Path) -> None:
    ledger = PublicAuditLedger(tmp_path, enabled=True)
    signal = _sample_signal()
    ledger.append_delivered(signal, tier="WATCH", message_id=1)
    ledger.append_delivered(signal, tier="action", message_id=2)
    assert len(ledger.recent_action_signals(within_hours=4.0)) == 1
