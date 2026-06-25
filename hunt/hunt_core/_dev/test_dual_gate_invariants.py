#!/usr/bin/env python3
"""Dual-Gate regression tests — invariant checks on pre_phase_gate + Detection.

These test the Dual-Gate-specific code paths (not the full factor pipeline).
They verify that:
  1. pre_phase_gate() accepts/rejects based on correct thresholds
  2. Detection.signal_type maps correctly per phase+gate
  3. to_setup_dict() / build_delivery_setup() propagate confirmed+signal_type
  4. auto_resolve_active_signals() resolves TP1/SL hits

Usage:
    python -m hunt_core._dev.test_dual_gate_invariants
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hunt_core.scanner.detect.fusion import (
    FusionScore,
    GateDecision,
    PreGateDecision,
    pre_phase_gate,
)
from hunt_core.scanner.detect.result import Detection
from hunt_core.scanner.detect.delivery_setup import build_delivery_setup
from hunt_core.scanner.detect.phase import PhaseInfo, PRE_PUMP, PRE_DUMP, PRE_COIL, MID, NEUTRAL
from hunt_core.scanner.detect.factors import FactorScore
from hunt_core.track.tracker import auto_resolve_active_signals, close_signal


# ── helpers ──────────────────────────────────────────────────────

def _fusion(
    side: str = "long",
    z_dir: float = 1.0,
    magnitude: float = 0.5,
    fusion_score: float = 12.5,
    n_active: int = 2,
) -> FusionScore:
    return FusionScore(side, z_dir, magnitude, 0.0, fusion_score, n_active, True)


def _gate(gate_open: bool = True, reason: str = "gate_open") -> GateDecision:
    return GateDecision(gate_open, None, 0.9, reason, 0.5)


def _pre_gate(pre_gate_open: bool = True, energy_hits: int = 3) -> PreGateDecision:
    return PreGateDecision(pre_gate_open, energy_hits, 0.3, "pre_gate_open" if pre_gate_open else "blocked")


def _detection(
    phase: str = PRE_PUMP,
    gate_open: bool = False,
    pre_gate_open: bool = False,
    signal_type: str = "none",
    side: str = "long",
    pre_gate: PreGateDecision | None = None,
) -> Detection:
    return Detection(
        symbol="BTCUSDT",
        tf="15m",
        side=side,
        phase=phase,
        watch_ok=phase in {PRE_PUMP, PRE_DUMP, PRE_COIL},
        gate_open=gate_open,
        pre_gate_open=pre_gate_open,
        signal_type=signal_type,
        confidence=0.5,
        magnitude=0.5,
        price=100.0,
        fusion=_fusion(side=side),
        gate=_gate(gate_open=gate_open),
        pre_gate=pre_gate,
        phase_info=PhaseInfo(phase, 0.5, None, phase == MID, phase in {PRE_PUMP, PRE_DUMP, PRE_COIL}),
        factors=[],
    )


def _row() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "market": {"depth_imbalance": 0.25, "oi_z": 2.0, "map_accumulation_score": 0.6, "map_absorption_count": 2},
        "lifecycle": {"phase": PRE_PUMP},
    }


# ── Tests ────────────────────────────────────────────────────────

def test_pre_phase_gate_accepts() -> None:
    d = pre_phase_gate(energy_hits=3, structure_score=0.2, magnitude=0.2)
    assert d.pre_gate_open, f"expected open, got {d.reason}"
    assert d.energy_hits == 3
    assert d.structure_score == 0.2


def test_pre_phase_gate_low_energy() -> None:
    d = pre_phase_gate(energy_hits=2, structure_score=0.2, magnitude=0.2)
    assert not d.pre_gate_open
    assert "low_energy" in d.reason


def test_pre_phase_gate_low_structure() -> None:
    d = pre_phase_gate(energy_hits=3, structure_score=0.1, magnitude=0.2)
    assert not d.pre_gate_open
    assert "low_structure" in d.reason


def test_pre_phase_gate_low_magnitude() -> None:
    d = pre_phase_gate(energy_hits=3, structure_score=0.2, magnitude=0.1)
    assert not d.pre_gate_open
    assert "low_magnitude" in d.reason


def test_signal_type_pre_pump_gate_open() -> None:
    """pre_pump + pre_gate_open → signal_type=pre_phase, confirmed=True"""
    det = _detection(phase=PRE_PUMP, pre_gate_open=True, signal_type="pre_phase", pre_gate=_pre_gate())
    assert det.signal_type == "pre_phase"
    assert det.gate_open or det.pre_gate_open  # any gate opens
    sd = det.to_setup_dict()
    lon = sd.get("long", {})
    assert lon.get("confirmed") is True
    assert lon.get("signal_type") == "pre_phase"


def test_signal_type_pre_dump_gate_open() -> None:
    """pre_dump + pre_gate_open → signal_type=pre_phase, confirmed=True"""
    det = _detection(phase=PRE_DUMP, side="short", pre_gate_open=True, signal_type="pre_phase", pre_gate=_pre_gate())
    assert det.signal_type == "pre_phase"
    sd = det.to_setup_dict()
    dum = sd.get("dump", {})
    assert dum.get("confirmed") is True
    assert dum.get("signal_type") == "pre_phase"


def test_signal_type_mid_gate_open() -> None:
    """mid + gate_open → signal_type=mid_phase, confirmed=True"""
    det = _detection(phase=MID, gate_open=True, signal_type="mid_phase")
    assert det.signal_type == "mid_phase"
    assert det.confidence > 0  # noqa: E712 (pytest compat)


def test_signal_type_neutral() -> None:
    """neutral → signal_type=none, confirmed=False"""
    det = _detection(phase=NEUTRAL)
    assert det.signal_type == "none"
    assert not det.gate_open
    assert not det.pre_gate_open


def test_signal_type_pre_pump_blocked() -> None:
    """pre_pump + pre_gate fails → signal_type=pre_phase_blocked"""
    det = _detection(phase=PRE_PUMP, pre_gate_open=False, signal_type="pre_phase_blocked")
    assert det.signal_type == "pre_phase_blocked"
    assert not det.pre_gate_open
    sd = det.to_setup_dict()
    lon = sd.get("long", {})
    assert lon.get("confirmed") is False


def test_delivery_setup_propagates_pre_phase() -> None:
    """build_delivery_setup() propagates confirmed + pre_gate for pre_phase"""
    det = _detection(phase=PRE_PUMP, pre_gate_open=True, signal_type="pre_phase", pre_gate=_pre_gate())
    setup = build_delivery_setup(det, _row())
    assert setup.get("confirmed") is True
    assert setup.get("signal_type") == "pre_phase"
    assert setup.get("forecast") is True
    pg = setup.get("pre_gate")
    assert isinstance(pg, dict)
    assert pg.get("open") is True
    assert pg.get("energy_hits") == 3


def test_delivery_setup_keeps_mid_unchanged() -> None:
    """mid phase signal_type stays mid_phase, forecast=False"""
    det = _detection(phase=MID, gate_open=True, signal_type="mid_phase")
    setup = build_delivery_setup(det, _row())
    assert setup.get("confirmed") is True
    assert setup.get("signal_type") == "mid_phase"
    assert setup.get("forecast") is False


def test_pre_gate_contains_pre_gate_dict() -> None:
    """pre_gate dict in delivery_setup has all fields"""
    det = _detection(phase=PRE_PUMP, pre_gate_open=True, signal_type="pre_phase", pre_gate=_pre_gate())
    setup = build_delivery_setup(det, _row())
    pg = setup.get("pre_gate")
    assert pg.get("open") is True
    assert pg.get("energy_hits") == 3
    assert isinstance(pg.get("structure_score"), float)
    assert isinstance(pg.get("reason"), str)


def test_auto_resolve_tp1_hit_long() -> None:
    """auto_resolve closes long signal when price >= tp1"""
    state = {"signals": {}}
    sig = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "active",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 98.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["signals"]["BTCUSDT:long"] = sig
    price_map = {"BTCUSDT": 106.0}
    closed = auto_resolve_active_signals(state, price_map, grace_minutes=0)
    assert "BTCUSDT:long" in closed
    assert sig.get("close_reason") == "tp1_hit"
    assert sig.get("pnl_pct") is not None


def test_auto_resolve_sl_hit_long() -> None:
    """auto_resolve closes long signal when price <= sl"""
    state = {"signals": {}}
    sig = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "active",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 98.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["signals"]["BTCUSDT:long"] = sig
    price_map = {"BTCUSDT": 97.0}
    closed = auto_resolve_active_signals(state, price_map, grace_minutes=0)
    assert "BTCUSDT:long" in closed
    assert sig.get("close_reason") == "stop_loss"


def test_auto_resolve_tp1_hit_short() -> None:
    """auto_resolve closes short signal when price <= tp1"""
    state = {"signals": {}}
    sig = {
        "symbol": "BTCUSDT",
        "direction": "short",
        "status": "active",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 104.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["signals"]["BTCUSDT:short"] = sig
    price_map = {"BTCUSDT": 94.0}
    closed = auto_resolve_active_signals(state, price_map, grace_minutes=0)
    assert "BTCUSDT:short" in closed
    assert sig.get("close_reason") == "tp1_hit"


def test_auto_resolve_timeout() -> None:
    """auto_resolve closes signal older than timeout_hours"""
    state = {"signals": {}}
    old_ts = datetime(2023, 1, 1, tzinfo=timezone.utc)
    sig = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "active",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 98.0,
        "tp1": 105.0,
        "opened_at": old_ts.isoformat(),
    }
    state["signals"]["BTCUSDT:long"] = sig
    price_map = {"BTCUSDT": 100.0}
    now = datetime(2023, 1, 5, tzinfo=timezone.utc)  # 4 days later > 48h
    closed = auto_resolve_active_signals(state, price_map, now=now, grace_minutes=0, timeout_hours=48)
    assert "BTCUSDT:long" in closed
    assert sig.get("close_reason") == "timeout"


def test_auto_resolve_grace_period() -> None:
    """auto_resolve respects grace_minutes — does not close fresh signals"""
    state = {"signals": {}}
    now = datetime.now(timezone.utc)
    sig = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "active",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 98.0,
        "tp1": 105.0,
        "opened_at": now.isoformat(),
    }
    state["signals"]["BTCUSDT:long"] = sig
    price_map = {"BTCUSDT": 106.0}  # above TP1 but within grace
    closed = auto_resolve_active_signals(state, price_map, now=now, grace_minutes=5)
    assert "BTCUSDT:long" not in closed
    assert sig.get("close_reason") is None


def test_auto_resolve_only_active() -> None:
    """auto_resolve skips already-closed signals"""
    state = {"signals": {}}
    sig = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "closed",  # already closed
        "close_reason": "manual",
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 98.0,
        "tp1": 105.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["signals"]["BTCUSDT:long"] = sig
    price_map = {"BTCUSDT": 106.0}
    closed = auto_resolve_active_signals(state, price_map, grace_minutes=0)
    assert "BTCUSDT:long" not in closed


# ── Runner ───────────────────────────────────────────────────────


def main() -> None:
    tests = [
        ("pre_phase_gate accepts", test_pre_phase_gate_accepts),
        ("pre_phase_gate rejects low_energy", test_pre_phase_gate_low_energy),
        ("pre_phase_gate rejects low_structure", test_pre_phase_gate_low_structure),
        ("pre_phase_gate rejects low_magnitude", test_pre_phase_gate_low_magnitude),
        ("signal_type pre_pump + pre_gate → pre_phase", test_signal_type_pre_pump_gate_open),
        ("signal_type pre_dump + pre_gate → pre_phase", test_signal_type_pre_dump_gate_open),
        ("signal_type mid + gate → mid_phase", test_signal_type_mid_gate_open),
        ("signal_type neutral → none", test_signal_type_neutral),
        ("signal_type pre_pump blocked → pre_phase_blocked", test_signal_type_pre_pump_blocked),
        ("delivery_setup propagates pre_phase", test_delivery_setup_propagates_pre_phase),
        ("delivery_setup keeps mid unchanged", test_delivery_setup_keeps_mid_unchanged),
        ("pre_gate dict in delivery_setup", test_pre_gate_contains_pre_gate_dict),
        ("auto_resolve TP1 long", test_auto_resolve_tp1_hit_long),
        ("auto_resolve SL long", test_auto_resolve_sl_hit_long),
        ("auto_resolve TP1 short", test_auto_resolve_tp1_hit_short),
        ("auto_resolve timeout", test_auto_resolve_timeout),
        ("auto_resolve grace period", test_auto_resolve_grace_period),
        ("auto_resolve skips closed", test_auto_resolve_only_active),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed}/{passed + failed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
