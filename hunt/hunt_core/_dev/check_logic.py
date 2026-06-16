"""Offline logic self-checks — replacement for removed verify CLI (P11/E1)."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

from hunt_core.contract import validate_signal_contract
from hunt_core.confluence.confluence import evaluate_must_pass, family_vote_count, FAMILY_VOTE_MIN
from hunt_core.confluence.mtf import MTFConfluence, ScenarioScore
from hunt_core.domain.config import load_config_defaults_toml, load_toml_defaults
from hunt_core.levels.levels import MIN_RR
from hunt_core.regime.classifier import Regime


def _sample_signal() -> SimpleNamespace:
    return SimpleNamespace(
        direction="short",
        entry_low=100.0,
        entry_high=101.0,
        stop_loss=105.0,
        tp1=95.0,
        tp2=90.0,
        tp3=90.0,
        scale_weights=(0.5, 0.5),
        valid_until=datetime.now(UTC) + timedelta(hours=12),
    )


def main() -> int:
    issues: list[str] = []

    sig = _sample_signal()
    contract_issues = validate_signal_contract(sig, min_risk_reward=1.0)
    if contract_issues:
        issues.append(f"contract expected pass got {contract_issues}")

    defaults = load_toml_defaults()
    if not isinstance(defaults, dict):
        issues.append("load_toml_defaults not dict")

    row = {
        "lifecycle": {"recommended_bias": "long"},
        "dump": {"dump_score": 80, "confirmed": True},
    }
    ok, missing = evaluate_must_pass(row, direction="short")
    if ok or "htf_bias_veto" not in missing:
        issues.append(f"must_pass veto expected got ok={ok} missing={missing}")

    mtf = MTFConfluence(
        symbol="T",
        price=1.0,
        tf_signals={},
        long_scenario=ScenarioScore("long", 0.4, 0, 3, 0, 0, 0, 0, 0),
        short_scenario=ScenarioScore("short", 0.5, 0, 3, 0, 0, 0, 0, 0),
        dominant="neutral",
    )
    if family_vote_count(mtf, direction="short") >= FAMILY_VOTE_MIN:
        issues.append("family_vote low fixture expected")

    bad_rr = _sample_signal()
    bad_rr.stop_loss = 100.5
    bad_issues = validate_signal_contract(bad_rr, min_risk_reward=1.5)
    if not bad_issues:
        issues.append("contract expected RR fail")

    if MIN_RR < 1.5:
        issues.append(f"levels MIN_RR expected >=1.5 got {MIN_RR}")

    from hunt_core.deliver.dispatch import _contract_issues_for_setup, _repair_setup_rr_for_contract

    repair_setup = {
        "confirmed": True,
        "entry_zone": [0.98, 1.0],
        "stop_loss": 1.02,
        "tp1": 0.99,
        "tp2": 0.97,
        "impulse_low": 0.95,
    }
    _repair_setup_rr_for_contract(repair_setup, direction="short", min_rr=1.15)
    repair_issues = _contract_issues_for_setup(
        direction="short",
        setup=repair_setup,
        min_risk_reward=1.15,
    )
    if repair_issues:
        issues.append(f"rr repair expected pass got {repair_issues}")

    if Regime.RANGE.value != "range":
        issues.append("regime enum drift")

    if not load_config_defaults_toml():
        issues.append("config.defaults.toml empty or missing")

    if issues:
        for item in issues:
            print(f"FAIL {item}", file=sys.stderr)
        return 1
    print("check_logic ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
