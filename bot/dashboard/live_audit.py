"""Minimal live audit snapshot for dashboard ``/api/live/audit``.

WS health and zero-hit setup alerts live in ``operator_alerts.py``; CLI checks in
``scripts/live_check_*.py``. This module only builds the audit-tab report dict.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

JsonDict = dict[str, Any]
_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True, slots=True)
class AuditFinding:
    severity: str
    area: str
    code: str
    title: str
    detail: str
    evidence: JsonDict = field(default_factory=dict)
    recommendation: str = ""


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe(findings: list[AuditFinding], ok: bool, finding: AuditFinding) -> None:
    if not ok:
        findings.append(finding)


def _collect_findings(snap: Mapping[str, Any]) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    ov = snap.get("overview") or {}
    sl = snap.get("shortlist") or {}
    fn = snap.get("funnel") or {}
    dec = snap.get("decisions") or {}
    rej = snap.get("rejections") or {}
    deliv = snap.get("delivery") or {}
    tg = snap.get("telegram") or {}
    rt = snap.get("runtime") or {}
    totals = fn.get("cycle_totals") or {}
    cycles, detectors = _int(totals.get("cycles")), _int(totals.get("detector_runs"))
    candidates, delivered = _int(totals.get("candidates")), _int(totals.get("delivered"))
    signal_rate = _float((fn.get("decisions") or {}).get("signal_rate"))
    total_sl, dynamic_sl = _int(sl.get("total")), _int(sl.get("dynamic"))
    zero_fit = _int(sl.get("zero_fit"))
    source = str(sl.get("source") or "unknown")
    last_c, last_d = _int(ov.get("last_cycle_candidates")), _int(ov.get("last_cycle_delivered"))
    dec_rows, dec_rate = _int(ov.get("decision_rows")), _float(ov.get("decision_signal_rate"))

    _maybe(
        out,
        bool(ov.get("running", True)),
        AuditFinding(
            "critical",
            "overview",
            "bot_not_running",
            "Bot is stopped.",
            "Telemetry may be historical only.",
            {"running": False},
            "Start runtime before drawing signal-rate conclusions.",
        ),
    )
    _maybe(
        out,
        not (dec_rows > 0 and dec_rate > 0 and last_c <= 0),
        AuditFinding(
            "warning",
            "overview",
            "raw_hits_not_selected",
            "Raw hits are not reaching candidate selection.",
            "Filters or selection layers absorb detector output.",
            {
                "decision_rows": dec_rows,
                "decision_signal_rate": round(dec_rate, 4),
                "last_cycle_candidates": last_c,
            },
            "Inspect rejection stages before changing detectors.",
        ),
    )
    _maybe(
        out,
        not (last_c > 0 and last_d <= 0),
        AuditFinding(
            "warning",
            "overview",
            "selection_delivery_gap",
            "Last cycle selected candidates but delivered none.",
            "Delivery gates, cooldown, or notifier may be blocking sends.",
            {"last_cycle_candidates": last_c, "last_cycle_delivered": last_d},
            "Inspect Delivery panel and notifier configuration.",
        ),
    )
    top_reason = str((ov.get("top_rejection") or {}).get("key") or "")
    if top_reason:
        out.append(
            AuditFinding(
                "info",
                "overview",
                "top_rejection_visible",
                f"Top rejection: {top_reason}.",
                "Review rejection telemetry before changing thresholds.",
                {"reason": top_reason, "count": (ov.get("top_rejection") or {}).get("count")},
                "Classify root cause from rejection examples.",
            )
        )
    _maybe(
        out,
        not (total_sl and total_sl < 30),
        AuditFinding(
            "critical",
            "shortlist",
            "shortlist_too_small",
            "Shortlist too small.",
            "Analysis surface limits detector runs.",
            {"total": total_sl, "source": source},
            "Check REST full refresh and universe thresholds.",
        ),
    )
    _maybe(
        out,
        not (dynamic_sl and dynamic_sl < 30),
        AuditFinding(
            "warning",
            "shortlist",
            "dynamic_pool_small",
            "Dynamic pool below expected size.",
            "Pinned symbols alone may be insufficient.",
            {"dynamic": dynamic_sl, "source": source},
            "Prefer REST full shortlist during startup.",
        ),
    )
    _maybe(
        out,
        not (total_sl and zero_fit > total_sl * 0.25),
        AuditFinding(
            "critical",
            "shortlist",
            "strategy_routing_empty",
            "Many symbols lack strategy_fits.",
            "Empty routing skips detectors.",
            {"zero_fit": zero_fit, "total": total_sl},
            "Inspect universe routing and enabled setup ids.",
        ),
    )
    if source == "cached":
        out.append(
            AuditFinding(
                "info",
                "shortlist",
                "cached_shortlist_active",
                "Cached shortlist source active.",
                "Normal when ws_light is too small.",
                {"source": source, "total": total_sl},
                "Confirm periodic rest_full refresh succeeds.",
            )
        )
    _maybe(
        out,
        not (cycles > 0 and detectors <= 0),
        AuditFinding(
            "critical",
            "funnel",
            "no_detector_runs",
            "Cycles without detector runs.",
            "Engine is not invoked.",
            {"cycles": cycles, "detector_runs": detectors},
            "Check shortlist routing and frame readiness.",
        ),
    )
    _maybe(
        out,
        not (detectors > 0 and signal_rate <= 0.0),
        AuditFinding(
            "critical",
            "funnel",
            "zero_raw_signal_rate",
            "Zero raw signal rate.",
            "Strategy/data gates may block before filters.",
            {"detector_runs": detectors, "signal_rate": signal_rate},
            "Use strategy_decisions blockers by setup.",
        ),
    )
    _maybe(
        out,
        not (detectors > 0 and candidates <= 0),
        AuditFinding(
            "warning",
            "funnel",
            "zero_post_filter_candidates",
            "No post-filter candidates.",
            "Global filters may be too strict.",
            {"detector_runs": detectors, "candidates": candidates},
            "Review rejected.jsonl and filter reasons.",
        ),
    )
    _maybe(
        out,
        not (candidates > 0 and delivered <= 0),
        AuditFinding(
            "warning",
            "funnel",
            "candidate_delivery_gap",
            "Candidates without delivery.",
            "Selection or notifier policy may suppress sends.",
            {"candidates": candidates, "delivered": delivered},
            "Inspect delivery telemetry.",
        ),
    )
    reasons = list(rej.get("reasons") or [])
    if reasons:
        total_rej = _int(rej.get("total_rows"))
        reason = str(reasons[0].get("key") or "")
        count = _int(reasons[0].get("count"))
        pct = count / max(total_rej, 1)
        sev = "critical" if pct >= 0.35 else "warning" if pct >= 0.15 else "info"
        out.append(
            AuditFinding(
                sev,
                "rejections",
                "dominant_rejection",
                f"Dominant rejection: {reason}.",
                "High share of rejections from one reason.",
                {"reason": reason, "count": count, "pct": round(pct * 100, 2)},
                "Inspect rejection telemetry before loosening gates.",
            )
        )
    dec_total = _int(dec.get("total_rows"))
    dec_signals = _int((dec.get("status_counts") or {}).get("signal"))
    if dec_total <= 0:
        out.append(
            AuditFinding(
                "critical",
                "decisions",
                "no_strategy_decision_telemetry",
                "No decision telemetry.",
                "Cannot explain signal rate without strategy_decisions rows.",
                {},
                "Verify engine emits StrategyDecision rows.",
            )
        )
    elif dec_signals <= 0:
        out.append(
            AuditFinding(
                "critical",
                "decisions",
                "no_raw_signals",
                "No raw signals in decisions.",
                "Detector layer rejects before global filters.",
                {"total_rows": dec_total},
                "Use zero-hit alerts and setup blockers for detail.",
            )
        )
    sel, dcnt = _int(deliv.get("selected_count")), _int(deliv.get("delivery_count"))
    if sel <= 0 and dcnt <= 0:
        out.append(
            AuditFinding(
                "info",
                "delivery",
                "no_delivery_rows",
                "No delivery telemetry in scope.",
                "Expected when candidates never reach selection.",
                {"selected_count": sel, "delivery_count": dcnt},
                "Use decision/rejection panels until rows appear.",
            )
        )
    elif dcnt <= 0:
        out.append(
            AuditFinding(
                "warning",
                "delivery",
                "selected_not_delivered",
                "Selected but not delivered.",
                "Notifier or cooldown may suppress sends.",
                {
                    "selected_count": sel,
                    "delivery_status_counts": deliv.get("delivery_status_counts") or {},
                },
                "Inspect delivery_orchestrator and notifier.",
            )
        )
    if not tg.get("available"):
        out.append(
            AuditFinding(
                "info",
                "telegram",
                "preview_unavailable",
                "Telegram preview unavailable.",
                "Preview needs candidate/selected rows.",
                {"reason": tg.get("reason")},
                "Run pipeline check with candidates to inspect message.",
            )
        )
    else:
        preview = tg.get("preview") or {}
        if not preview.get("ok", False):
            out.append(
                AuditFinding(
                    "critical",
                    "telegram",
                    "telegram_html_invalid",
                    "Telegram HTML validation failed.",
                    "Invalid HTML causes parse errors.",
                    {"errors": preview.get("errors"), "warnings": preview.get("warnings")},
                    "Escape dynamic fields; use supported Telegram HTML tags only.",
                )
            )
        if _int(preview.get("chars")) > 3900:
            out.append(
                AuditFinding(
                    "warning",
                    "telegram",
                    "telegram_message_near_limit",
                    "Message near Telegram limit.",
                    "Long messages risk send failure.",
                    {"chars": preview.get("chars")},
                    "Keep main signal compact.",
                )
            )
    ws = rt.get("ws_snapshot") or {}
    if ws and _int(ws.get("fresh_tickers")) <= 0:
        out.append(
            AuditFinding(
                "warning",
                "runtime",
                "ws_ticker_cache_cold",
                "Ticker cache has no fresh rows.",
                "ws_light shortlist may degrade.",
                {"fresh_tickers": ws.get("fresh_tickers")},
                "Check WS subscriptions (see also /api/v1/alerts).",
            )
        )
    if ws and _int(ws.get("fresh_mark_prices")) <= 0:
        out.append(
            AuditFinding(
                "warning",
                "runtime",
                "mark_price_cache_cold",
                "Mark price cache has no fresh rows.",
                "Mark sanity checks may use REST fallback.",
                {"fresh_mark_prices": ws.get("fresh_mark_prices")},
                "Check mark-price stream health.",
            )
        )
    diag = rt.get("signal_diagnostics") or {}
    dr, dh = _int(diag.get("detector_runs_total")), _int(diag.get("detector_hits_total"))
    if dr > 0 and dh <= 0:
        out.append(
            AuditFinding(
                "warning",
                "runtime",
                "diagnostics_zero_hits",
                "Diagnostics: runs with zero hits.",
                "Current-window detector activity with no hits.",
                {"detector_runs_total": dr, "detector_hits_total": dh},
                "Compare with decision telemetry.",
            )
        )
    return out


def _sort(findings: list[AuditFinding]) -> list[AuditFinding]:
    return sorted(findings, key=lambda f: (_SEV_RANK.get(f.severity, 3), f.area, f.code))


def _health_score(findings: list[AuditFinding]) -> int:
    score = 100
    for item in findings:
        score -= {"critical": 25, "warning": 10, "info": 2}.get(item.severity, 0)
    return max(0, min(100, score))


def _action_plan(findings: list[AuditFinding], *, limit: int = 6) -> list[str]:
    seen: set[str] = set()
    plan: list[str] = []
    for item in _sort(findings):
        rec = item.recommendation.strip()
        if not rec:
            continue
        key = f"{item.area}:{item.code}:{rec}"
        if key in seen:
            continue
        seen.add(key)
        plan.append(f"{item.area}/{item.code}: {rec}")
        if len(plan) >= limit:
            break
    return plan


def build_dashboard_audit_snapshot(
    *,
    overview: Mapping[str, Any],
    funnel: Mapping[str, Any],
    shortlist: Mapping[str, Any],
    decisions: Mapping[str, Any],
    rejections: Mapping[str, Any],
    delivery: Mapping[str, Any],
    runtime: Mapping[str, Any],
    telegram: Mapping[str, Any],
) -> JsonDict:
    """Normalized telemetry bundle for ``audit_snapshot``."""
    return {
        "overview": dict(overview),
        "funnel": dict(funnel),
        "shortlist": dict(shortlist),
        "decisions": dict(decisions),
        "rejections": dict(rejections),
        "delivery": dict(delivery),
        "runtime": dict(runtime),
        "telegram": dict(telegram),
    }


def audit_snapshot(snapshot: Mapping[str, Any]) -> JsonDict:
    """Build the audit-tab API payload from a dashboard snapshot."""
    findings = _sort(_collect_findings(snapshot))
    plan = _action_plan(findings)
    by_sev = Counter(f.severity for f in findings)
    top = findings[0] if findings else None
    brief = (
        "Live audit found no dashboard-visible runtime issues."
        if not findings
        else " ".join(
            [
                f"Live audit: {by_sev.get('critical', 0)} critical, {by_sev.get('warning', 0)} warning, "
                f"health score {_health_score(findings)}/100.",
                f"Top finding: {top.area}/{top.code} - {top.title}" if top else "",
                f"First action: {plan[0]}" if plan else "",
            ]
        ).strip()
    )
    status = (
        "critical" if by_sev.get("critical") else "degraded" if by_sev.get("warning") else "healthy"
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "summary": {
            "total": len(findings),
            "by_severity": dict(by_sev),
            "by_area": dict(Counter(f.area for f in findings)),
            "critical": by_sev.get("critical", 0),
            "warning": by_sev.get("warning", 0),
            "info": by_sev.get("info", 0),
        },
        "score": _health_score(findings),
        "action_plan": plan,
        "operator_brief": brief,
        "findings": [asdict(f) for f in findings],
    }
