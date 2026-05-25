"""Live telemetry audit helpers for the dashboard and operator checks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One live runtime finding."""

    severity: str
    area: str
    code: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Structured live audit report."""

    generated_at: str
    status: str
    findings: tuple[AuditFinding, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        action_plan = build_action_plan(self.findings)
        return {
            "generated_at": self.generated_at,
            "status": self.status,
            "summary": self.summary,
            "score": health_score(self.findings),
            "action_plan": action_plan,
            "operator_brief": format_operator_brief(self.findings, action_plan=action_plan),
            "findings": [
                {
                    "severity": item.severity,
                    "area": item.area,
                    "code": item.code,
                    "title": item.title,
                    "detail": item.detail,
                    "evidence": item.evidence,
                    "recommendation": item.recommendation,
                }
                for item in self.findings
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Live Runtime Audit",
            "",
            f"- Generated: `{self.generated_at}`",
            f"- Status: `{self.status}`",
            f"- Health score: `{health_score(self.findings)}`",
            f"- Findings: `{len(self.findings)}`",
            "",
        ]
        action_plan = build_action_plan(self.findings)
        if action_plan:
            lines.extend(["## Operator Action Plan", ""])
            for index, item in enumerate(action_plan, start=1):
                lines.append(f"{index}. {item}")
            lines.append("")
        for item in self.findings:
            lines.extend(
                [
                    f"## {item.severity.upper()} / {item.area} / {item.code}",
                    item.title,
                    "",
                    item.detail,
                    "",
                ]
            )
            if item.evidence:
                lines.append("Evidence:")
                for key, value in item.evidence.items():
                    lines.append(f"- `{key}`: `{value}`")
                lines.append("")
            if item.recommendation:
                lines.extend(["Recommendation:", item.recommendation, ""])
        return "\n".join(lines).rstrip() + "\n"


REJECTION_CLASS_RULES: tuple[tuple[str, str, str], ...] = (
    ("spread", "execution_quality", "Spread or book quality blocked the candidate."),
    ("stale_", "data_freshness", "Required timeframe data was stale."),
    ("atr_too_low", "volatility_floor", "Static volatility floor blocked the candidate."),
    ("atr_expansion", "volatility_pattern", "ATR expansion strategy did not confirm."),
    ("volume_too_low", "participation", "Volume participation was too low."),
    ("average_volume_too_low", "participation", "Average volume gate rejected the setup."),
    ("volume_confirmation_missing", "participation", "Required volume confirmation was absent."),
    ("no_bounce", "pattern_absent", "Bounce pattern was absent."),
    ("no_breakout", "pattern_absent", "Breakout pattern was absent."),
    ("no_order_block", "pattern_absent", "Order block pattern was absent."),
    ("no_hidden_divergence", "pattern_absent", "Hidden divergence was absent."),
    ("no_liquidity_sweep", "pattern_absent", "Liquidity sweep was absent."),
    ("no_wick_trap", "pattern_absent", "Wick trap was absent."),
    ("funding", "sentiment_context", "Funding context did not confirm."),
    ("ls_ratio", "sentiment_context", "Long/short ratio context did not confirm."),
    ("oi_", "open_interest_context", "Open interest context did not confirm."),
    ("flow_precheck", "orderflow_context", "Orderflow precheck opposed the direction."),
    ("depth", "orderbook_context", "Depth/order-book context did not confirm."),
    ("wall", "orderbook_context", "Whale-wall context was too weak."),
    ("btc_correlation", "market_context", "BTC correlation did not align."),
    ("benchmark", "market_context", "Benchmark context made the setup non-actionable."),
    ("altcoin", "market_context", "Altcoin-season context did not confirm."),
    ("risk_reward", "risk_plan", "Risk/reward did not pass."),
    ("score_too_low", "score_floor", "Confluence score did not pass."),
    ("cooldown", "delivery_policy", "Cooldown blocked delivery."),
    ("quality_monitor", "delivery_policy", "Quality monitor blocked delivery."),
    ("open_signal", "delivery_policy", "Existing signal blocked delivery."),
)


SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "warning": 1,
    "info": 2,
}


def classify_rejection(reason: str) -> dict[str, str]:
    """Classify a rejection reason into an operational family."""
    normalized = str(reason or "").strip().lower()
    for needle, family, detail in REJECTION_CLASS_RULES:
        if needle in normalized:
            return {"family": family, "detail": detail}
    if normalized.startswith("pattern."):
        return {"family": "pattern_absent", "detail": "Detector pattern did not confirm."}
    if normalized.startswith("indicator."):
        return {"family": "indicator_context", "detail": "Indicator context did not confirm."}
    if normalized.startswith("data."):
        return {"family": "missing_source_data", "detail": "Required data context was missing."}
    if normalized.startswith("filter."):
        return {"family": "global_filter", "detail": "Global filter blocked the candidate."}
    return {"family": "other", "detail": "Unclassified rejection reason."}


def severity_rank(severity: str) -> int:
    """Return an ordering rank for a finding severity."""
    return SEVERITY_ORDER.get(str(severity or "").lower(), 3)


def sort_findings(findings: Iterable[AuditFinding]) -> list[AuditFinding]:
    """Sort findings by severity, then operational area and code."""
    return sorted(
        findings,
        key=lambda item: (severity_rank(item.severity), item.area, item.code),
    )


def health_score(findings: Iterable[AuditFinding]) -> int:
    """Compute a compact 0-100 health score from findings.

    The score is intentionally heuristic and observation-only. It exists to
    make dashboard regressions visible at a glance without hiding the concrete
    findings that explain the number.
    """
    score = 100
    for item in findings:
        if item.severity == "critical":
            score -= 25
        elif item.severity == "warning":
            score -= 10
        elif item.severity == "info":
            score -= 2
    return max(0, min(100, score))


def build_action_plan(findings: Iterable[AuditFinding], *, limit: int = 6) -> list[str]:
    """Build a short ordered action list from the highest-priority findings."""
    actions: list[str] = []
    seen: set[str] = set()
    for item in sort_findings(findings):
        recommendation = item.recommendation.strip()
        if not recommendation:
            continue
        key = f"{item.area}:{item.code}:{recommendation}"
        if key in seen:
            continue
        seen.add(key)
        actions.append(f"{item.area}/{item.code}: {recommendation}")
        if len(actions) >= max(1, int(limit)):
            break
    return actions


def format_operator_brief(
    findings: Iterable[AuditFinding],
    *,
    action_plan: Iterable[str] | None = None,
) -> str:
    """Return a compact human-readable brief for dashboard operators."""
    ordered = sort_findings(findings)
    if not ordered:
        return "Live audit found no dashboard-visible runtime issues."
    critical = sum(1 for item in ordered if item.severity == "critical")
    warning = sum(1 for item in ordered if item.severity == "warning")
    top = ordered[0]
    lines = [
        f"Live audit: {critical} critical, {warning} warning, health score {health_score(ordered)}/100.",
        f"Top finding: {top.area}/{top.code} - {top.title}",
    ]
    actions = list(action_plan or build_action_plan(ordered))
    if actions:
        lines.append(f"First action: {actions[0]}")
    return " ".join(lines)


def rejection_family_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate rejection rows by operational family."""
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for row in rows:
        reason = str(row.get("key") or row.get("reason") or "")
        count = int(row.get("count") or 1)
        family = classify_rejection(reason)["family"]
        counts[family] += count
        examples.setdefault(family, [])
        if reason and reason not in examples[family] and len(examples[family]) < 5:
            examples[family].append(reason)
    return [
        {
            "family": family,
            "count": int(count),
            "examples": examples.get(family, []),
        }
        for family, count in counts.most_common()
    ]


class LiveTelemetryAuditor:
    """Audit live dashboard summaries and telemetry aggregates."""

    def audit(self, snapshot: Mapping[str, Any]) -> AuditReport:
        findings: list[AuditFinding] = []
        findings.extend(self.audit_overview(snapshot.get("overview") or {}))
        findings.extend(self.audit_shortlist(snapshot.get("shortlist") or {}))
        findings.extend(self.audit_funnel(snapshot.get("funnel") or {}))
        findings.extend(self.audit_rejections(snapshot.get("rejections") or {}))
        findings.extend(self.audit_decisions(snapshot.get("decisions") or {}))
        findings.extend(self.audit_delivery(snapshot.get("delivery") or {}))
        findings.extend(self.audit_telegram(snapshot.get("telegram") or {}))
        findings.extend(self.audit_runtime(snapshot.get("runtime") or {}))
        status = self._status_from_findings(findings)
        return AuditReport(
            generated_at=datetime.now(UTC).isoformat(),
            status=status,
            findings=tuple(sort_findings(findings)),
            summary=self._summary(findings),
        )

    def audit_overview(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        running = bool(data.get("running", True))
        if not running:
            findings.append(
                AuditFinding(
                    "critical",
                    "overview",
                    "bot_not_running",
                    "Dashboard reports the bot as stopped.",
                    "The dashboard can still show historical telemetry, but live analytics are not running.",
                    {"running": running},
                    "Start the runtime before using dashboard data for signal-rate conclusions.",
                )
            )
        decision_rows = self._int(data.get("decision_rows"))
        decision_rate = self._float(data.get("decision_signal_rate"))
        last_candidates = self._int(data.get("last_cycle_candidates"))
        last_delivered = self._int(data.get("last_cycle_delivered"))
        if decision_rows > 0 and decision_rate > 0 and last_candidates <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "overview",
                    "raw_hits_not_selected",
                    "Raw detector hits are not reaching candidate selection.",
                    "Strategies are active, but filter/confirmation/selection layers are absorbing the output.",
                    {
                        "decision_rows": decision_rows,
                        "decision_signal_rate": round(decision_rate, 4),
                        "last_cycle_candidates": last_candidates,
                    },
                    "Use rejection-stage and top-reason panels before changing detector logic.",
                )
            )
        if last_candidates > 0 and last_delivered <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "overview",
                    "selection_delivery_gap",
                    "Last cycle selected candidates but delivered none.",
                    "Notifier provider, cooldown, open-signal policy, or delivery gates may be blocking sends.",
                    {
                        "last_cycle_candidates": last_candidates,
                        "last_cycle_delivered": last_delivered,
                    },
                    "Inspect Delivery rows and notifier provider configuration.",
                )
            )
        top_rejection = data.get("top_rejection") or {}
        top_reason = str(top_rejection.get("key") or "")
        if top_reason:
            classification = classify_rejection(top_reason)
            findings.append(
                AuditFinding(
                    "info",
                    "overview",
                    "top_rejection_visible",
                    f"Top rejection is {top_reason}.",
                    classification["detail"],
                    {
                        "reason": top_reason,
                        "family": classification["family"],
                        "count": top_rejection.get("count"),
                    },
                    self._recommend_for_rejection_family(classification["family"]),
                )
            )
        return findings

    def audit_shortlist(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        total = self._int(data.get("total"))
        dynamic = self._int(data.get("dynamic"))
        zero_fit = self._int(data.get("zero_fit"))
        source = str(data.get("source") or "unknown")
        if total and total < 30:
            findings.append(
                AuditFinding(
                    "critical",
                    "shortlist",
                    "shortlist_too_small",
                    "Shortlist has too few symbols.",
                    "The analysis surface is small enough to reduce detector runs and signal rate.",
                    {"total": total, "source": source},
                    "Check REST full refresh and universe thresholds before judging strategies.",
                )
            )
        if dynamic and dynamic < 30:
            findings.append(
                AuditFinding(
                    "warning",
                    "shortlist",
                    "dynamic_pool_small",
                    "Dynamic shortlist pool is below expected size.",
                    "Pinned symbols alone are not enough for a broad futures signal bot.",
                    {"dynamic": dynamic, "source": source},
                    "Prefer REST full shortlist evidence over ws_light during startup.",
                )
            )
        if total and zero_fit > total * 0.25:
            findings.append(
                AuditFinding(
                    "critical",
                    "shortlist",
                    "strategy_routing_empty",
                    "Many shortlist symbols have zero strategy_fits.",
                    "Symbols with empty routing do not run detectors.",
                    {"zero_fit": zero_fit, "total": total},
                    "Inspect universe._strategy_fits_for_row() and enabled setup ids.",
                )
            )
        if source == "cached":
            findings.append(
                AuditFinding(
                    "info",
                    "shortlist",
                    "cached_shortlist_active",
                    "Dashboard is showing cached shortlist source.",
                    "This can be healthy when ws_light is rejected as too small.",
                    {"source": source, "total": total},
                    "Confirm a periodic rest_full refresh succeeds in shortlist telemetry.",
                )
            )
        return findings

    def audit_funnel(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        totals = data.get("cycle_totals") or {}
        cycles = self._int(totals.get("cycles"))
        detectors = self._int(totals.get("detector_runs"))
        candidates = self._int(totals.get("candidates"))
        delivered = self._int(totals.get("delivered"))
        decisions = data.get("decisions") or {}
        signal_rate = self._float(decisions.get("signal_rate"))
        if cycles > 0 and detectors <= 0:
            findings.append(
                AuditFinding(
                    "critical",
                    "funnel",
                    "no_detector_runs",
                    "Cycles are running without detector runs.",
                    "The bot cannot generate signals if the engine is not invoked.",
                    {"cycles": cycles, "detector_runs": detectors},
                    "Check shortlist routing, frame readiness, and engine invocation.",
                )
            )
        if detectors > 0 and signal_rate <= 0.0:
            findings.append(
                AuditFinding(
                    "critical",
                    "funnel",
                    "zero_raw_signal_rate",
                    "Detector surface has zero raw signals.",
                    "This points to strategy/data contracts or market-condition gates before filters.",
                    {"detector_runs": detectors, "signal_rate": signal_rate},
                    "Use strategy_decisions top blockers by setup to classify the cause.",
                )
            )
        if detectors > 0 and candidates <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "funnel",
                    "zero_post_filter_candidates",
                    "Raw detector activity is not reaching post-filter candidates.",
                    "Global filters or confirmation gates may be too strict for current market.",
                    {"detector_runs": detectors, "candidates": candidates},
                    "Review rejected.jsonl stages and top global filter reasons.",
                )
            )
        if candidates > 0 and delivered <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "funnel",
                    "candidate_delivery_gap",
                    "Candidates exist but no delivery occurred.",
                    "Selection, cooldown, open-signal, or notifier policy may be suppressing sends.",
                    {"candidates": candidates, "delivered": delivered},
                    "Inspect delivery telemetry and selection rejection rows.",
                )
            )
        return findings

    def audit_rejections(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        total = self._int(data.get("total_rows"))
        reasons = list(data.get("reasons") or [])
        if not reasons:
            return findings
        top = reasons[0]
        reason = str(top.get("key") or "")
        count = self._int(top.get("count"))
        pct = count / max(total, 1)
        classification = classify_rejection(reason)
        severity = "critical" if pct >= 0.35 else "warning" if pct >= 0.15 else "info"
        findings.append(
            AuditFinding(
                severity,
                "rejections",
                "dominant_rejection",
                f"Dominant rejection: {reason}",
                classification["detail"],
                {
                    "reason": reason,
                    "count": count,
                    "pct": round(pct * 100.0, 2),
                    "family": classification["family"],
                },
                self._recommend_for_rejection_family(classification["family"]),
            )
        )
        for item in reasons[:8]:
            family = classify_rejection(str(item.get("key") or ""))["family"]
            if family == "data_freshness":
                findings.append(
                    AuditFinding(
                        "warning",
                        "rejections",
                        "freshness_rejections_present",
                        "Freshness gates are rejecting candidates.",
                        "Stale timeframe data can hide working strategies.",
                        {"reason": item.get("key"), "count": item.get("count")},
                        "Inspect WS kline freshness and REST frame cache latency.",
                    )
                )
                break
        family_rows = rejection_family_rows(reasons[:20])
        if family_rows:
            dominant = family_rows[0]
            if dominant["family"] in {"execution_quality", "data_freshness"}:
                findings.append(
                    AuditFinding(
                        "warning",
                        "rejections",
                        "dominant_family_operational",
                        f"Dominant rejection family is {dominant['family']}.",
                        "The rejection cluster points to runtime/data quality more than strategy logic.",
                        {
                            "family": dominant["family"],
                            "count": dominant["count"],
                            "examples": dominant["examples"],
                        },
                        self._recommend_for_rejection_family(str(dominant["family"])),
                    )
                )
        return findings

    def audit_decisions(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        total = self._int(data.get("total_rows"))
        status_counts = data.get("status_counts") or {}
        signals = self._int(status_counts.get("signal"))
        zero = list(data.get("zero_signal_setups") or [])
        if total <= 0:
            findings.append(
                AuditFinding(
                    "critical",
                    "decisions",
                    "no_strategy_decision_telemetry",
                    "No strategy decision telemetry found.",
                    "Dashboard cannot explain signal rate without strategy_decisions rows.",
                    {},
                    "Verify engine emits StrategyDecision telemetry for every detector run.",
                )
            )
        elif signals <= 0:
            findings.append(
                AuditFinding(
                    "critical",
                    "decisions",
                    "no_raw_signals",
                    "Strategy decisions contain no raw signals.",
                    "The detector layer is rejecting everything before global filters.",
                    {"total_rows": total},
                    "Classify each zero-hit strategy by top blocker, not by metadata status.",
                )
            )
        if zero:
            findings.append(
                AuditFinding(
                    "warning",
                    "decisions",
                    "zero_signal_setups",
                    "Some setups have decision rows but zero raw signals.",
                    "This may be market condition, missing source data, or detector bug.",
                    {"count": len(zero), "setups": [row.get("setup_id") for row in zero[:10]]},
                    "Inspect the setup-specific top blockers and live feature contract.",
                )
            )
        return findings

    def audit_delivery(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        selected = self._int(data.get("selected_count"))
        delivered = self._int(data.get("delivery_count"))
        statuses = data.get("delivery_status_counts") or {}
        if selected <= 0 and delivered <= 0:
            findings.append(
                AuditFinding(
                    "info",
                    "delivery",
                    "no_delivery_rows",
                    "No selected/delivery telemetry in current scope.",
                    "This is expected when candidates never reach selection, but bad for operator visibility.",
                    {"selected_count": selected, "delivery_count": delivered},
                    "Use decision and rejection views as the source of truth until selected rows appear.",
                )
            )
        elif delivered <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "delivery",
                    "selected_not_delivered",
                    "Selected signals did not create delivery rows.",
                    "Notifier, cooldown, or tracking policy may be suppressing sends.",
                    {"selected_count": selected, "delivery_status_counts": statuses},
                    "Inspect delivery_orchestrator rejected rows and notifier provider.",
                )
            )
        return findings

    def audit_telegram(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        if not data.get("available"):
            findings.append(
                AuditFinding(
                    "info",
                    "telegram",
                    "preview_unavailable",
                    "No signal-like row was available for Telegram preview.",
                    "This dashboard can still show the formatter once selected/candidate rows exist.",
                    {"reason": data.get("reason")},
                    "Run a live pipeline check that yields candidates to inspect the exact message.",
                )
            )
            return findings
        preview = data.get("preview") or {}
        if not preview.get("ok", False):
            findings.append(
                AuditFinding(
                    "critical",
                    "telegram",
                    "telegram_html_invalid",
                    "Telegram preview failed HTML validation.",
                    "Invalid HTML causes Telegram parse errors or plain-text fallback.",
                    {"errors": preview.get("errors"), "warnings": preview.get("warnings")},
                    "Escape user/market data and keep only Telegram-supported HTML tags.",
                )
            )
        if self._int(preview.get("chars")) > 3900:
            findings.append(
                AuditFinding(
                    "warning",
                    "telegram",
                    "telegram_message_near_limit",
                    "Telegram message is close to the text limit.",
                    "Long messages are harder to scan and risk parse/send failure.",
                    {"chars": preview.get("chars")},
                    "Keep main signal compact; put analytics in companion messages or dashboard.",
                )
            )
        return findings

    def audit_runtime(self, data: Mapping[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        ws = data.get("ws_snapshot") or {}
        fresh_tickers = self._int(ws.get("fresh_tickers"))
        fresh_mark = self._int(ws.get("fresh_mark_prices"))
        if ws and fresh_tickers <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "runtime",
                    "ws_ticker_cache_cold",
                    "WebSocket ticker cache has no fresh rows.",
                    "ws_light shortlist and mark/ticker comparisons may degrade.",
                    {"fresh_tickers": fresh_tickers},
                    "Check websocket subscriptions and Binance stream health.",
                )
            )
        if ws and fresh_mark <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "runtime",
                    "mark_price_cache_cold",
                    "Mark price cache has no fresh rows.",
                    "Mark-price sanity checks may degrade or rely on REST fallback.",
                    {"fresh_mark_prices": fresh_mark},
                    "Check mark-price stream and REST fallback state.",
                )
            )
        diagnostics = data.get("signal_diagnostics") or {}
        runs = self._int(diagnostics.get("detector_runs_total"))
        hits = self._int(diagnostics.get("detector_hits_total"))
        if runs > 0 and hits <= 0:
            findings.append(
                AuditFinding(
                    "warning",
                    "runtime",
                    "diagnostics_zero_hits",
                    "In-process diagnostics show detector runs with zero hits.",
                    "This is a current-window problem, not only historical telemetry.",
                    {"detector_runs_total": runs, "detector_hits_total": hits},
                    "Compare dashboard decision telemetry with in-process diagnostics.",
                )
            )
        return findings

    def _recommend_for_rejection_family(self, family: str) -> str:
        recommendations = {
            "execution_quality": "Do not loosen blindly; inspect spread source, book freshness, and symbol liquidity.",
            "data_freshness": "Fix frame freshness and websocket/cache timing before changing strategy thresholds.",
            "volatility_floor": "Use regime-aware ATR diagnostics; avoid raising min_atr_pct.",
            "volatility_pattern": "Classify as market condition unless the detector never hits across expanded live samples.",
            "participation": "Check whether volume thresholds are setup-specific and whether volume_ratio is populated.",
            "pattern_absent": "Use expanded live samples before calling this a bug; pattern absence may be market condition.",
            "sentiment_context": "Verify futures-data REST caches for funding and long/short ratios.",
            "open_interest_context": "Verify OI history warmup and current/public endpoint availability.",
            "orderflow_context": "Compare aggTrade/depth freshness with fallback proxies.",
            "orderbook_context": "Check L2 depth subscriptions for tracked symbols and L1 proxy labeling.",
            "market_context": "Verify benchmark context and cross-asset market updater.",
            "risk_plan": "Inspect stop/target geometry; do not lower RR without checking target integrity.",
            "score_floor": "Use confluence component diagnostics before changing min_score.",
            "delivery_policy": "Inspect active signals, cooldowns, quality monitor, and notifier provider.",
        }
        return recommendations.get(family, "Inspect examples and classify root cause before changing behavior.")

    def _summary(self, findings: Iterable[AuditFinding]) -> dict[str, Any]:
        findings = tuple(findings)
        by_severity = Counter(item.severity for item in findings)
        by_area = Counter(item.area for item in findings)
        return {
            "total": len(findings),
            "by_severity": dict(by_severity),
            "by_area": dict(by_area),
            "critical": by_severity.get("critical", 0),
            "warning": by_severity.get("warning", 0),
            "info": by_severity.get("info", 0),
        }

    def _status_from_findings(self, findings: Iterable[AuditFinding]) -> str:
        severities = {item.severity for item in findings}
        if "critical" in severities:
            return "critical"
        if "warning" in severities:
            return "degraded"
        return "healthy"

    def _int(self, value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


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
) -> dict[str, Any]:
    """Build the normalized snapshot consumed by ``LiveTelemetryAuditor``."""
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


def audit_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Audit a normalized dashboard snapshot and return a dict report."""
    return LiveTelemetryAuditor().audit(snapshot).to_dict()


def audit_snapshot_markdown(snapshot: Mapping[str, Any]) -> str:
    """Audit a normalized dashboard snapshot and return Markdown."""
    return LiveTelemetryAuditor().audit(snapshot).to_markdown()
