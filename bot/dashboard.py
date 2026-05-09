"""FastAPI dashboard for signal bot monitoring."""

from __future__ import annotations

import json
import logging
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

UTC = timezone.utc

if TYPE_CHECKING:
    from fastapi import FastAPI

try:
    from fastapi import FastAPI as _FastAPI
    from fastapi.middleware.cors import CORSMiddleware as _CORSMiddleware
    from fastapi.responses import HTMLResponse as _HTMLResponse

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

LOG = logging.getLogger("bot.dashboard")


class BotDashboard:
    """FastAPI dashboard bound to the current bot process."""

    def __init__(self, bot: Any, port: int = 8080, host: str = "127.0.0.1") -> None:
        self.bot = bot
        self.port = port
        self.host = host
        self._enabled = HAS_FASTAPI
        self.app: FastAPI | None = None
        self._strategies_cache: list[dict[str, Any]] | None = None

        if not self._enabled:
            LOG.warning("fastapi not installed, dashboard disabled")
            return

        app = _FastAPI(title="Signal Bot Dashboard", version="2.0.0")
        app.add_middleware(
            _CORSMiddleware,
            allow_origins=[
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            ],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.app = app
        self._setup_routes()
        self._cache_strategies()

    def _setup_routes(self) -> None:
        if not self.app:
            return

        @self.app.get("/", response_class=_HTMLResponse)
        async def root() -> str:
            return self._get_html_dashboard()

        @self.app.get("/api/status")
        async def status() -> dict[str, Any]:
            return await self._get_status()

        @self.app.get("/api/signals/active")
        async def active_signals() -> list[dict[str, Any]]:
            return await self._get_active_signals()

        @self.app.get("/api/signals/recent")
        async def recent_signals(limit: int = 20) -> list[dict[str, Any]]:
            return self._get_recent_signals(limit)

        @self.app.get("/api/market/regime")
        async def market_regime() -> dict[str, Any]:
            return self._get_market_regime()

        @self.app.get("/api/metrics")
        async def metrics() -> dict[str, Any]:
            return await self._get_metrics()

        @self.app.get("/api/health")
        async def health() -> dict[str, Any]:
            return await self.bot.health_check()

        @self.app.get("/api/analytics/report")
        async def analytics_report(days: int = 30) -> dict[str, Any]:
            from .analytics import StrategyAnalytics

            days = max(1, min(int(days), 365))
            reporter = StrategyAnalytics(repo=self.bot._modern_repo)
            report = await reporter.generate_report(days=days)
            return self._merge_strategy_catalog(report)

        @self.app.get("/api/strategies")
        async def strategies() -> list[dict[str, Any]]:
            """Return cached list of strategies with their enabled status."""
            if self._strategies_cache is not None:
                return self._strategies_cache
            return []

    def _cache_strategies(self) -> None:
        """Pre-load and cache strategies at startup."""
        try:
            from .strategies import STRATEGY_CLASSES

            settings = getattr(self.bot, "settings", None)
            setups = getattr(settings, "setups", None)
            if hasattr(setups, "enabled_setup_ids"):
                enabled_setups = set(setups.enabled_setup_ids())
            else:
                enabled_setups = {
                    key
                    for key in dir(setups or object())
                    if not key.startswith("_") and bool(getattr(setups, key, False))
                }

            self._strategies_cache = []
            for cls in STRATEGY_CLASSES:
                setup_id = str(getattr(cls, "setup_id", "") or cls.__name__)
                self._strategies_cache.append(
                    {
                        "id": setup_id,
                        "name": cls.__name__,
                        "enabled": setup_id in enabled_setups,
                        "status": str(getattr(cls, "status", "beta")),
                        "risk_profile": str(
                            getattr(
                                cls, "risk_profile", getattr(cls, "family", "generic")
                            )
                        ),
                        "family": str(getattr(cls, "family", "generic")),
                    }
                )
            LOG.info("dashboard cached %d strategies", len(self._strategies_cache))
        except Exception as exc:
            LOG.warning("failed to cache strategies: %s", exc)
            self._strategies_cache = []

    def _merge_strategy_catalog(self, report: dict[str, Any]) -> dict[str, Any]:
        """Attach every registered strategy to analytics, including zero-outcome rows."""
        catalog = self._strategies_cache or []
        by_setup = dict(report.get("by_setup") or {})
        rows_by_setup = {
            str(row.get("setup_id")): dict(row)
            for row in report.get("setup_reports", [])
            if row.get("setup_id")
        }
        for item in catalog:
            setup_id = str(item.get("id") or "")
            if not setup_id:
                continue
            row = rows_by_setup.setdefault(
                setup_id,
                {
                    "setup_id": setup_id,
                    "trades": 0,
                    "count": 0,
                    "win_rate": 0.0,
                    "expectancy_r": 0.0,
                    "avg_rr": 0.0,
                    "profit_factor": None,
                    "max_drawdown_r": 0.0,
                },
            )
            row["enabled"] = bool(item.get("enabled"))
            row["status"] = item.get("status", "beta")
            row["risk_profile"] = item.get("risk_profile", "generic")
            row["family"] = item.get("family", "generic")
            by_setup[setup_id] = row
        setup_reports = sorted(
            rows_by_setup.values(),
            key=lambda row: (
                not bool(row.get("enabled", True)),
                -int(row.get("trades") or 0),
                str(row.get("status") or ""),
                str(row.get("setup_id") or ""),
            ),
        )
        report["by_setup"] = by_setup
        report["setup_reports"] = setup_reports
        report["registered_strategies"] = len(catalog)
        report["enabled_strategies"] = sum(1 for item in catalog if item.get("enabled"))
        return report

    def _get_html_dashboard(self) -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Signal Bot Dashboard</title>
    <style>
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border-color: #30363d;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --accent-blue: #58a6ff;
            --accent-green: #3fb950;
            --accent-red: #f85149;
            --accent-orange: #f0883e;
            --accent-purple: #a371f7;
            --accent-yellow: #d29922;
            --font-mono: 'SF Mono', Monaco, Inconsolata, 'Roboto Mono', monospace;
            --radius: 8px;
            --shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }
        .header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: 16px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header h1 {
            font-size: 20px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .header .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
            background: var(--bg-tertiary);
        }
        .status-badge.online { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .status-badge.offline { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .status-badge::before {
            content: '';
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .nav-tabs {
            display: flex;
            gap: 8px;
            background: var(--bg-tertiary);
            padding: 6px;
            border-radius: var(--radius);
        }
        .nav-tab {
            padding: 8px 16px;
            border: none;
            background: transparent;
            color: var(--text-secondary);
            font-size: 14px;
            cursor: pointer;
            border-radius: 6px;
            transition: all 0.2s;
        }
        .nav-tab:hover { color: var(--text-primary); }
        .nav-tab.active { background: var(--bg-secondary); color: var(--text-primary); }
        .main { padding: 24px; max-width: 1400px; margin: 0 auto; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }
        .card {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: 20px;
            box-shadow: var(--shadow);
        }
        .card h2 {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .metric-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-color);
        }
        .metric-row:last-child { border-bottom: none; }
        .metric-label { color: var(--text-secondary); font-size: 14px; }
        .metric-value {
            font-family: var(--font-mono);
            font-weight: 600;
            font-size: 14px;
        }
        .metric-value.green { color: var(--accent-green); }
        .metric-value.red { color: var(--accent-red); }
        .metric-value.blue { color: var(--accent-blue); }
        .metric-value.yellow { color: var(--accent-yellow); }
        .metric-value.purple { color: var(--accent-purple); }
        .signal-card {
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: 16px;
            margin-bottom: 12px;
            transition: all 0.2s;
        }
        .signal-card:hover { border-color: var(--accent-blue); }
        .signal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .signal-symbol {
            font-family: var(--font-mono);
            font-size: 16px;
            font-weight: 600;
        }
        .signal-direction {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .signal-direction.long { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .signal-direction.short { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .signal-details {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 12px;
        }
        .signal-detail {
            text-align: center;
        }
        .signal-detail-label {
            font-size: 11px;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-bottom: 4px;
        }
        .signal-detail-value {
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 500;
        }
        .signal-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 12px;
            border-top: 1px solid var(--border-color);
        }
        .signal-setup {
            font-size: 12px;
            color: var(--text-secondary);
        }
        .signal-score {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }
        .score-high { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .score-medium { background: rgba(210,153,34,0.15); color: var(--accent-yellow); }
        .score-low { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        .empty-state-icon {
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.5;
        }
        .last-update {
            position: fixed;
            bottom: 16px;
            right: 16px;
            padding: 8px 16px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            font-size: 12px;
            color: var(--text-secondary);
        }
        .toast-container {
            position: fixed;
            top: 80px;
            right: 24px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            z-index: 200;
        }
        .toast {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: 16px 20px;
            box-shadow: var(--shadow);
            animation: slideIn 0.3s ease;
            min-width: 280px;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .toast-title { font-weight: 600; margin-bottom: 4px; }
        .toast-body { font-size: 13px; color: var(--text-secondary); }
        .keyboard-hint {
            position: fixed;
            bottom: 16px;
            left: 16px;
            font-size: 12px;
            color: var(--text-secondary);
        }
        .keyboard-hint kbd {
            background: var(--bg-tertiary);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: var(--font-mono);
        }
        @media (max-width: 768px) {
            .header { flex-direction: column; gap: 12px; }
            .nav-tabs { width: 100%; overflow-x: auto; }
            .main { padding: 16px; }
            .signal-details { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header class="header">
        <h1>
            <span>Signal Bot</span>
            <span id="status-badge" class="status-badge offline">Offline</span>
        </h1>
        <nav class="nav-tabs">
            <button class="nav-tab active" data-tab="overview">Overview</button>
            <button class="nav-tab" data-tab="signals">Signals</button>
            <button class="nav-tab" data-tab="analytics">Analytics</button>
            <button class="nav-tab" data-tab="settings">Settings</button>
        </nav>
    </header>
    
    <main class="main">
        <!-- Overview Tab -->
        <div id="tab-overview" class="tab-content active">
            <div class="grid">
                <div class="card">
                    <h2>Bot Status</h2>
                    <div class="metric-row">
                        <span class="metric-label">Shortlist Size</span>
                        <span id="shortlist" class="metric-value blue">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Open Signals</span>
                        <span id="signals" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">WS Latency</span>
                        <span id="ws-latency" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Market Regime</span>
                        <span id="market" class="metric-value">-</span>
                    </div>
                </div>
                <div class="card">
                    <h2>Market Context</h2>
                    <div class="metric-row">
                        <span class="metric-label">BTC Bias</span>
                        <span id="btc-bias" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Market Strength</span>
                        <span id="market-strength" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Active Strategies</span>
                        <span id="active-strategies" class="metric-value green">-</span>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2>Recent Activity</h2>
                <div id="recent-activity">
                    <div class="empty-state">
                        <div class="empty-state-icon">Activity</div>
                        <p>No recent activity</p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Signals Tab -->
        <div id="tab-signals" class="tab-content">
            <div id="active-signals-list">
                <div class="empty-state">
                    <div class="empty-state-icon">Radio</div>
                    <p>No active signals</p>
                </div>
            </div>
        </div>
        
        <!-- Analytics Tab -->
        <div id="tab-analytics" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h2>Performance (30d)</h2>
                    <div class="metric-row">
                        <span class="metric-label">Total Signals</span>
                        <span id="total-signals" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Win Rate</span>
                        <span id="win-rate" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Avg R/R</span>
                        <span id="avg-rr" class="metric-value">-</span>
                    </div>
                </div>
                <div class="card">
                    <h2>Signal Distribution</h2>
                    <div id="signal-chart" style="height: 200px; display: flex; align-items: flex-end; justify-content: center; gap: 8px; padding: 20px;">
                        <div style="text-align: center; color: var(--text-secondary);">Loading chart...</div>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2>Strategy Performance</h2>
                <div id="strategy-chart" style="min-height: 150px;">
                    <div class="empty-state">
                        <p>Strategy performance data will appear here</p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content">
            <div class="card">
                <h2>Strategy Configuration</h2>
                <div id="strategy-list">
                    <p class="empty-state">Loading strategies...</p>
                </div>
            </div>
        </div>
    </main>
    
    <div class="toast-container" id="toast-container"></div>
    
    <div class="last-update" id="last-update">Last update: -</div>
    
    <div class="keyboard-hint">
        <kbd>?</kbd> for help
    </div>
    
    <script>
        // Tab switching
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
            });
        });
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.key === '?') {
                showToast('Keyboard Shortcuts', '1-4: Switch tabs | R: Refresh | ?: Help');
            }
            if (e.key >= '1' && e.key <= '4') {
                const tabs = ['overview', 'signals', 'analytics', 'settings'];
                document.querySelector(`[data-tab="${tabs[e.key - 1]}"]`).click();
            }
            if (e.key === 'r' || e.key === 'R') {
                fetchStatus();
                fetchSignals();
                showToast('Refreshed', 'Data updated manually');
            }
        });
        
        // Toast notifications
        function showToast(title, body) {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `<div class="toast-title">${title}</div><div class="toast-body">${body}</div>`;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 5000);
        }
        
        // Formatters
        const fmt = {
            price: (v) => v ? v.toFixed(4) : '-',
            pct: (v) => v ? (v * 100).toFixed(1) + '%' : '-',
            score: (v) => {
                if (!v) return { text: '-', class: '' };
                if (v >= 0.75) return { text: (v * 100).toFixed(0) + '%', class: 'score-high' };
                if (v >= 0.60) return { text: (v * 100).toFixed(0) + '%', class: 'score-medium' };
                return { text: (v * 100).toFixed(0) + '%', class: 'score-low' };
            }
        };
        
        // Fetch status
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                // Update status badge
                const badge = document.getElementById('status-badge');
                badge.textContent = data.running ? 'Online' : 'Offline';
                badge.className = 'status-badge ' + (data.running ? 'online' : 'offline');
                
                // Update metrics
                document.getElementById('shortlist').textContent = data.shortlist_size;
                document.getElementById('signals').textContent = data.open_signals;
                document.getElementById('ws-latency').textContent = data.ws_latency_ms + ' ms';
                
                const regimeEl = document.getElementById('market');
                regimeEl.textContent = data.market_regime;
                regimeEl.className = 'metric-value ' + (
                    data.market_regime === 'bull' ? 'green' : 
                    data.market_regime === 'bear' ? 'red' : 'yellow'
                );
                
                document.getElementById('market-strength').textContent = fmt.pct(data.market_strength);
                
                document.getElementById('last-update').textContent = 'Last update: ' + new Date().toLocaleTimeString();
            } catch (err) {
                console.error('Failed to fetch status:', err);
            }
        }
        
        // Fetch active signals
        async function fetchSignals() {
            try {
                const res = await fetch('/api/signals/active');
                const data = await res.json();
                const container = document.getElementById('active-signals-list');
                
                if (data.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">Radio</div>
                            <p>No active signals</p>
                        </div>`;
                    return;
                }
                
                container.innerHTML = data.map(s => {
                    const score = fmt.score(s.score);
                    return `
                        <div class="signal-card">
                            <div class="signal-header">
                                <span class="signal-symbol">${s.symbol}</span>
                                <span class="signal-direction ${s.direction.toLowerCase()}">${s.direction}</span>
                            </div>
                            <div class="signal-details">
                                <div class="signal-detail">
                                    <div class="signal-detail-label">Entry</div>
                                    <div class="signal-detail-value">${fmt.price(s.entry_price)}</div>
                                </div>
                                <div class="signal-detail">
                                    <div class="signal-detail-label">Stop</div>
                                    <div class="signal-detail-value" style="color: var(--accent-red)">${fmt.price(s.stop_price)}</div>
                                </div>
                                <div class="signal-detail">
                                    <div class="signal-detail-label">TP1</div>
                                    <div class="signal-detail-value" style="color: var(--accent-green)">${fmt.price(s.tp1_price)}</div>
                                </div>
                            </div>
                            <div class="signal-footer">
                                <span class="signal-setup">${s.setup_id}</span>
                                <span class="signal-score ${score.class}">${score.text}</span>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                console.error('Failed to fetch signals:', err);
            }
        }

        // Fetch recent activity
        async function fetchRecentActivity() {
            try {
                const res = await fetch('/api/signals/recent?limit=10');
                const data = await res.json();
                const container = document.getElementById('recent-activity');

                if (!Array.isArray(data) || data.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">Activity</div>
                            <p>No recent activity</p>
                        </div>`;
                    return;
                }

                container.innerHTML = data.map(s => {
                    const score = fmt.score(s.score);
                    const when = s.ts || s.created_at || '';
                    return `
                        <div class="metric-row">
                            <span class="metric-label">${s.symbol || '-'} ${s.setup_id || ''} ${s.direction || ''}</span>
                            <span class="metric-value ${score.class}" title="${when}">${score.text}</span>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                console.error('Failed to fetch recent activity:', err);
            }
        }
        
        // Fetch strategies
        async function fetchStrategies() {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 3000);
                const res = await fetch('/api/strategies', { signal: controller.signal });
                clearTimeout(timeoutId);
                
                if (!res.ok) {
                    throw new Error('HTTP ' + res.status);
                }
                
                const data = await res.json();
                const container = document.getElementById('strategy-list');
                
                if (!Array.isArray(data) || data.length === 0) {
                    container.innerHTML = '<p class="empty-state">No strategies available</p>';
                    document.getElementById('active-strategies').textContent = '0/0';
                    return;
                }
                
                document.getElementById('active-strategies').textContent = data.filter(s => s.enabled).length + '/' + data.length;
                
                container.innerHTML = data.map(s => `
                    <div class="metric-row">
                        <span class="metric-label">${s.name}</span>
                        <span class="metric-value ${s.enabled ? 'green' : 'red'}">${s.enabled ? 'ON' : 'OFF'}</span>
                    </div>
                `).join('');
            } catch (err) {
                console.error('Failed to fetch strategies:', err);
                const container = document.getElementById('strategy-list');
                container.innerHTML = '<p class="empty-state">Failed to load strategies</p>';
            }
        }
        
        // Fetch analytics
        async function fetchAnalytics() {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 5000);
                const res = await fetch('/api/analytics/report?days=30', { signal: controller.signal });
                clearTimeout(timeoutId);
                
                if (!res.ok) {
                    throw new Error('HTTP ' + res.status);
                }
                
                const data = await res.json();
                if (data.summary) {
                    document.getElementById('total-signals').textContent = data.summary.total_signals || '-';
                    document.getElementById('win-rate').textContent = data.summary.win_rate ? (data.summary.win_rate * 100).toFixed(1) + '%' : '-';
                    document.getElementById('avg-rr').textContent = data.summary.avg_rr ? data.summary.avg_rr.toFixed(2) : '-';
                }
                
                // Render signal distribution chart
                if (data.by_setup) {
                    renderSignalChart(data.by_setup);
                }

                if (data.setup_reports) {
                    renderStrategyPerformance(data.setup_reports);
                }
            } catch (err) {
                console.error('Analytics not available:', err);
            }
        }
        
        // Render signal distribution bar chart
        function renderSignalChart(bySetup) {
            const container = document.getElementById('signal-chart');
            if (!bySetup || Object.keys(bySetup).length === 0) {
                container.innerHTML = '<div style="text-align: center; color: var(--text-secondary);">No data available</div>';
                return;
            }
            
            const setups = Object.entries(bySetup)
                .sort((a, b) => (b[1].count || 0) - (a[1].count || 0))
                .slice(0, 10);  // Top 10 setups
            
            const maxCount = Math.max(...setups.map(s => s[1].count || 0), 1);
            
            const bars = setups.map(([name, data]) => {
                const count = data.count || 0;
                const height = (count / maxCount * 100) || 0;
                const winRate = data.win_rate ? (data.win_rate * 100).toFixed(0) : '0';
                return `
                    <div style="display: flex; flex-direction: column; align-items: center; flex: 1; min-width: 40px;">
                        <div style="width: 100%; background: var(--accent-blue); border-radius: 4px 4px 0 0; height: ${height}px; min-height: 4px; position: relative;" title="${name}: ${count} signals, ${winRate}% win">
                        </div>
                        <div style="font-size: 10px; color: var(--text-secondary); margin-top: 4px; writing-mode: vertical-rl; text-orientation: mixed; transform: rotate(180deg); height: 60px; overflow: hidden; text-overflow: ellipsis;">${name.replace('Setup', '')}</div>
                    </div>
                `;
            }).join('');
            
            container.innerHTML = bars;
        }

        function renderStrategyPerformance(setupReports) {
            const container = document.getElementById('strategy-chart');
            if (!Array.isArray(setupReports) || setupReports.length === 0) {
                container.innerHTML = '<div class="empty-state"><p>No strategy performance data</p></div>';
                return;
            }

            const rows = setupReports
                .slice()
                .sort((a, b) => {
                    const enabledDelta = Number(b.enabled !== false) - Number(a.enabled !== false);
                    if (enabledDelta !== 0) return enabledDelta;
                    return (b.trades || 0) - (a.trades || 0);
                })
                .map(row => {
                    const winRate = ((row.win_rate || 0) * 100).toFixed(1) + '%';
                    const rr = Number(row.expectancy_r || 0).toFixed(2);
                    const valueClass = row.expectancy_r > 0 ? 'green' : (row.expectancy_r < 0 ? 'red' : 'yellow');
                    const status = row.status || 'beta';
                    const suffix = row.enabled === false ? ' off' : status;
                    return `
                        <div class="metric-row">
                            <span class="metric-label">${row.setup_id}</span>
                            <span class="metric-value ${valueClass}">${row.trades || 0} / ${winRate} / ${rr}R / ${suffix}</span>
                        </div>
                    `;
                }).join('');

            container.innerHTML = rows;
        }
        
        // Initial fetch
        fetchStatus();
        fetchSignals();
        fetchRecentActivity();
        fetchStrategies();
        fetchAnalytics();
        
        // Periodic updates
        setInterval(fetchStatus, 5000);
        setInterval(fetchSignals, 10000);
        setInterval(fetchRecentActivity, 10000);
        setInterval(fetchStrategies, 30000);  // Refresh strategies every 30s
        setInterval(fetchAnalytics, 30000);
        
        // Welcome toast
        setTimeout(() => {
            showToast('Dashboard Ready', 'Signal Bot is running');
        }, 1000);
    </script>
</body>
</html>"""

    async def _get_status(self) -> dict[str, Any]:
        bot = self.bot
        regime = bot.market_regime._last_result

        ws_lag = 0
        if bot._ws_manager is not None:
            stats = bot._ws_manager.get_stats()
            ws_lag = stats.get("avg_latency_overall_ms", 0) or 0

        # Quick count without full fetch - use len of cached data
        open_signals_count = 0
        try:
            import asyncio

            signals = await asyncio.wait_for(
                bot._modern_repo.get_active_signals(), timeout=1.0
            )
            open_signals_count = len(signals)
        except Exception:
            pass  # Don't block dashboard for signal count

        return {
            "running": not bot._shutdown.is_set(),
            "shortlist_size": len(bot._shortlist),
            "open_signals": open_signals_count,
            "ws_latency_ms": ws_lag,
            "market_regime": regime.regime if regime else "unknown",
            "market_strength": regime.strength if regime else 0.0,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def _get_active_signals(self) -> list[dict[str, Any]]:
        try:
            # Use timeout to prevent blocking dashboard
            import asyncio

            signals = await asyncio.wait_for(
                self.bot._modern_repo.get_active_signals(), timeout=2.0
            )
            return [
                {
                    "symbol": sig.get("symbol"),
                    "setup_id": sig.get("setup_id"),
                    "direction": sig.get("direction"),
                    "entry_price": sig.get("activation_price")
                    or sig.get("entry_price")
                    or sig.get("entry_mid"),
                    "stop_price": sig.get("stop_price") or sig.get("stop"),
                    "tp1_price": sig.get("tp1_price") or sig.get("take_profit_1"),
                    "tp2_price": sig.get("tp2_price") or sig.get("take_profit_2"),
                    "score": sig.get("score"),
                    "risk_reward": sig.get("risk_reward"),
                    "status": sig.get("status"),
                    "tracking_id": sig.get("tracking_id"),
                    "tracking_ref": sig.get("tracking_ref"),
                }
                for sig in signals
            ]
        except asyncio.TimeoutError:
            LOG.debug("timeout fetching active signals for dashboard")
            return []
        except Exception as exc:
            LOG.debug("error fetching active signals: %s", exc)
            return []

    def _get_recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        telemetry_dir = self.bot.settings.telemetry_dir
        signals: list[dict[str, Any]] = []

        candidates_file = self._latest_analysis_file(telemetry_dir, "candidates.jsonl")
        if candidates_file is None:
            return signals

        try:
            with candidates_file.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            for line in reversed(lines[-limit:]):
                if line.strip():
                    signals.append(json.loads(line))
        except Exception as exc:
            LOG.debug("failed to read recent candidates: %s", exc)

        return signals

    def _get_market_regime(self) -> dict[str, Any]:
        regime = self.bot.market_regime._last_result
        if not regime:
            return {"error": "No market data available"}
        return regime.to_dict()

    async def _get_metrics(self) -> dict[str, Any]:
        bot = self.bot

        # Get signal count with timeout
        open_signals_count = 0
        try:
            import asyncio

            signals = await asyncio.wait_for(
                bot._modern_repo.get_active_signals(), timeout=1.0
            )
            open_signals_count = len(signals)
        except Exception:
            pass

        # Get market regime safely
        regime_data = None
        try:
            if bot.market_regime._last_result:
                regime_data = bot.market_regime._last_result.to_dict()
        except Exception:
            pass

        # Get engine stats safely
        engine_stats = {}
        try:
            engine_stats = bot._modern_engine.get_engine_stats()
        except Exception:
            pass

        return {
            "shortlist_size": len(bot._shortlist),
            "open_signals": open_signals_count,
            "ws_streams": len(bot._ws_manager._symbols) if bot._ws_manager else 0,
            "market_regime": regime_data,
            "engine": engine_stats,
        }

    def _latest_analysis_file(self, telemetry_dir: Path, filename: str) -> Path | None:
        runs_dir = telemetry_dir / "runs"
        if not runs_dir.exists():
            return None
        candidates = sorted(
            runs_dir.glob(f"*/analysis/{filename}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def start_server(
        self, *, auto_open: bool = True, delay_seconds: float = 1.5
    ) -> None:
        if not self._enabled or not self.app:
            LOG.debug("dashboard server disabled (fastapi not installed)")
            return

        from threading import Thread

        def run_server() -> None:
            if self.app is None:
                return
            try:
                import uvicorn  # type: ignore
            except Exception as exc:
                LOG.warning("dashboard server failed to import uvicorn: %s", exc)
                return
            try:
                uvicorn.run(
                    self.app, host=self.host, port=self.port, log_level="warning"
                )
            except Exception as exc:
                LOG.warning("dashboard server crashed: %s", exc)

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        LOG.info("dashboard server started on port %d", self.port)

        if auto_open:
            self._schedule_browser_open(delay_seconds)

    def _schedule_browser_open(self, delay_seconds: float) -> None:
        """Open browser after server is ready."""
        import threading
        import time

        def open_browser() -> None:
            time.sleep(delay_seconds)
            host = "localhost" if self.host in ("0.0.0.0", "127.0.0.1") else self.host
            url = f"http://{host}:{self.port}"
            try:
                webbrowser.open(url, new=2)  # new=2 opens in new tab
                LOG.info("opened dashboard in browser: %s", url)
            except Exception as exc:
                LOG.debug("failed to open browser: %s", exc)
                LOG.info("dashboard available at: %s", url)

        threading.Thread(target=open_browser, daemon=True).start()
