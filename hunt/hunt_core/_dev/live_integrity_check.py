"""Live end-to-end integrity: data → indicators → routing → gates (one report)."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "PEPEUSDT")

_TF_KEYS = ("1m_closed", "5m_closed", "15m_closed", "1h_closed", "4h_closed")
_INDICATOR_KEYS = ("rsi14", "atr14", "close")
_MARKET_CORE = ("oi", "taker_5m", "top_ls_5m", "global_ls_5m")
_MARKET_FULL = ("funding_pct", "oi_chg_1h", "taker_1h", "basis_pct")


def _check_tf_indicators(tf: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for key in _TF_KEYS:
        blk = tf.get(key)
        if not isinstance(blk, dict) or blk.get("status") == "empty":
            issues.append(f"tf_missing:{key}")
            continue
        for ind in _INDICATOR_KEYS:
            val = blk.get(ind)
            if val is None:
                issues.append(f"indicator_missing:{key}.{ind}")
            elif ind in {"rsi14", "atr14", "close"} and float(val) <= 0 and ind != "rsi14":
                issues.append(f"indicator_zero:{key}.{ind}")
    return issues


def _check_market(market: dict[str, Any], *, strict_full: bool) -> list[str]:
    issues: list[str] = []
    for k in _MARKET_CORE:
        if market.get(k) is None:
            issues.append(f"market_missing:{k}")
    if strict_full:
        for k in _MARKET_FULL:
            if market.get(k) is None:
                issues.append(f"market_missing:{k}")
    liq = market.get("liquidation_score_5m")
    if liq is not None and float(liq) < -0.01:
        issues.append(f"liquidation_sentinel:{liq}")
    return issues


def _check_row(row: dict[str, Any], *, strict_full: bool) -> dict[str, Any]:
    sym = str(row.get("symbol") or "?")
    issues: list[str] = []
    checks: list[str] = []

    if row.get("error"):
        issues.append(f"probe_error:{row['error']}")
    price = float(row.get("price") or 0)
    if price <= 0:
        issues.append("price_missing")
    else:
        checks.append(f"price={price:.4g}")

    lc = row.get("lifecycle") or {}
    if not lc.get("phase"):
        issues.append("lifecycle_phase_missing")
    else:
        checks.append(f"phase={lc.get('phase')}")

    tf = row.get("timeframes") or {}
    issues.extend(_check_tf_indicators(tf))
    if not issues:
        checks.append("indicators_ok")

    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    m_issues = _check_market(market, strict_full=strict_full)
    issues.extend(m_issues)
    if not m_issues:
        checks.append("market_core_ok")

    dq = row.get("data_quality") or {}
    missing = dq.get("fields_missing") or []
    if missing and strict_full:
        issues.append(f"dq_missing:{missing[:6]}")
    bars = {k: dq.get(k) for k in ("bars_1m", "bars_5m", "bars_15m", "bars_1h", "bars_4h") if dq.get(k)}
    if bars:
        checks.append(f"bars={bars}")
    if dq.get("closed_5m_ok"):
        checks.append("closed_5m_ok")
    if dq.get("prepare_ok"):
        checks.append("prepare_ok")

    dump = row.get("dump") or {}
    long_s = row.get("long") or {}
    has_signal = bool(dump.get("confirmed") or long_s.get("confirmed"))
    has_fusion = bool(
        (dump.get("fusion_score") or 0) > 0
        or (long_s.get("fusion_score") or 0) > 0
        or dump.get("p_win")
        or long_s.get("p_win")
    )
    phase = str(lc.get("phase") or "neutral").lower()
    if not has_signal and not has_fusion:
        missing_scores = not dump.get("dump_score") and not long_s.get("long_score")
        if missing_scores and phase not in {"neutral", "unknown", ""}:
            issues.append("both_scores_zero")
    else:
        checks.append(
            f"fusion short={dump.get('confirmed')} long={long_s.get('confirmed')}"
        )

    from hunt_core.scanner.detect.routing import route_tick

    routes = route_tick(row)
    checks.append(f"routes={len(routes)}")
    for r in routes:
        if r.setup.get("ignition_score") is not None:
            checks.append(f"ignition={r.setup.get('ignition_score')}")

    audit = row.get("_signal_audit") or {}
    audit_issues = [f"audit:{x}" for x in (audit.get("issues") or [])]
    hard_issues = list(issues)
    # Confirm audit mismatches are gate/logic divergence, not missing data.
    data_issues = [x for x in hard_issues if not x.startswith("audit:")]
    for ai in audit_issues:
        checks.append(ai.replace("audit:", "audit_warn:"))

    struct = row.get("structure") or row.get("market_structure")
    if isinstance(struct, dict) and struct.get("bias"):
        checks.append(f"structure_bias={struct.get('bias')}")

    return {
        "symbol": sym,
        "ok": not data_issues,
        "data_ok": not data_issues,
        "audit_ok": not audit_issues,
        "issues": data_issues + audit_issues,
        "checks": checks,
        "confirmed_short": bool(dump.get("confirmed")),
        "confirmed_long": bool(long_s.get("confirmed")),
        "fuel_short": dump.get("dump_fuel"),
        "fuel_long": long_s.get("long_fuel"),
    }


async def _probe_all(symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.domain.config import load_settings
    from hunt_core.market.factory import create_hunt_market_plane_from_settings
    from hunt_core.runtime.symbol_probe import probe_symbol_signal

    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    out: list[dict[str, Any]] = []
    try:
        for sym in symbols:
            row = await probe_symbol_signal(
                sym,
                client=plane.client,
                auto_watchlist=False,
                stagger_ms=250,
            )
            out.append(row)
    finally:
        await plane.aclose()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live hunt integrity check")
    parser.add_argument("symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--strict-full", action="store_true", help="Require full-tier derivatives")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    rows = asyncio.run(_probe_all(symbols))
    reports = [_check_row(r, strict_full=args.strict_full) for r in rows]
    data_failed = [r for r in reports if not r.get("data_ok", r["ok"])]
    audit_failed = [r for r in reports if not r.get("audit_ok", True)]
    payload = {
        "symbols": list(symbols),
        "reports": reports,
        "data_all_ok": not data_failed,
        "audit_all_ok": not audit_failed,
        "all_ok": not data_failed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for rep in reports:
            data_ok = rep.get("data_ok", rep["ok"])
            status = "OK" if data_ok else "FAIL"
            audit_tag = "" if rep.get("audit_ok", True) else " [audit_warn]"
            print(f"{status}{audit_tag} {rep['symbol']}: {', '.join(rep['checks'][:10])}")
            for issue in rep["issues"]:
                prefix = "  !" if not issue.startswith("audit:") else "  ~"
                print(f"{prefix} {issue}")
        print(
            f"\nintegrity data: {len(reports) - len(data_failed)}/{len(reports)} ok"
            f" | audit: {len(reports) - len(audit_failed)}/{len(reports)} ok"
        )
    return 1 if data_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
