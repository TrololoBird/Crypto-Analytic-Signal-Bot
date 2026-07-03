from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from hunt_core.analyst.pipeline._helpers import safe_float_opt

@dataclass
class MacroDataSnapshot:
    btc_d: float | None = None
    btc_d_change_24h: float | None = None
    total3_cap: float | None = None
    total3_change_24h: float | None = None
    timestamp: float = 0.0


_cached: MacroDataSnapshot | None = None
_last_fetch: float = 0.0


def _json_get(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def _fetch_cmc(api_key: str, base_url: str) -> dict | None:
    url_latest = f"{base_url}/global-metrics/quotes/latest"
    headers = {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"} if api_key else {}
    return _json_get(url_latest, headers=headers)


def _fetch_coingecko() -> dict | None:
    url = "https://api.coingecko.com/api/v3/global"
    return _json_get(url)


def _parse_cmc(data: dict, api_key: str, base_url: str, now: float) -> MacroDataSnapshot | None:
    try:
        metrics = data.get("data", data)
        btc_d = safe_float_opt(metrics.get("btc_dominance"))

        quote = metrics.get("quote", {})
        if isinstance(quote, dict):
            usd = quote.get("USD", {})
        else:
            usd = {}
        total_mcap = safe_float_opt(usd.get("total_market_cap"))

        btc_d_chg: float | None = None
        total3: float | None = None
        total3_chg: float | None = None

        if api_key:
            url_hist = f"{base_url}/global-metrics/quotes/historical"
            params = f"?time_start={int(now - 86400)}&interval=1d"
            hist_headers = {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}
            hist_data = _json_get(url_hist + params, headers=hist_headers)
            if hist_data is not None:
                quotes_list = hist_data.get("data", {}).get("quotes", [])
                if len(quotes_list) >= 2:
                    q_24h = quotes_list[-2]
                    btc_d_24h = safe_float_opt(q_24h.get("btc_dominance"))
                    if btc_d is not None and btc_d_24h is not None and btc_d_24h > 0:
                        btc_d_chg = (btc_d - btc_d_24h) / btc_d_24h * 100.0

                    q_usd = q_24h.get("quote", {}).get("USD", {}) if isinstance(q_24h.get("quote"), dict) else {}
                    total_mcap_24h = safe_float_opt(q_usd.get("total_market_cap"))
                    if total_mcap is not None and total_mcap_24h is not None and total_mcap_24h > 0:
                        total3 = total_mcap
                        total3_24h_adj = total_mcap_24h
                        if total3_24h_adj > 0 and total3 is not None:
                            total3_chg = (total3 - total3_24h_adj) / total3_24h_adj * 100.0

        if total3 is None and total_mcap is not None:
            btc_mcap = safe_float_opt(metrics.get("btc_market_cap", usd.get("btc_market_cap")))
            eth_dom = safe_float_opt(metrics.get("eth_dominance"))
            stable_dom = safe_float_opt(metrics.get("stablecoin_dominance"))
            if btc_d and eth_dom and stable_dom and total_mcap:
                total3 = total_mcap * (1.0 - btc_d / 100.0 - eth_dom / 100.0 - stable_dom / 100.0)

        return MacroDataSnapshot(
            btc_d=btc_d,
            btc_d_change_24h=btc_d_chg,
            total3_cap=total3,
            total3_change_24h=total3_chg,
            timestamp=now,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_coingecko(data: dict, now: float) -> MacroDataSnapshot | None:
    try:
        glbl = data.get("data", data)
        btc_d = safe_float_opt(glbl.get("market_cap_percentage", {}).get("btc"))
        total_mcap = safe_float_opt(glbl.get("total_market_cap", {}).get("usd"))
        btc_mcap = safe_float_opt(glbl.get("market_cap_percentage", {}).get("btc"))
        eth_dom = safe_float_opt(glbl.get("market_cap_percentage", {}).get("eth"))

        total3: float | None = None
        if total_mcap is not None and btc_d is not None and eth_dom is not None:
            total3 = total_mcap * (1.0 - btc_d / 100.0 - eth_dom / 100.0)

        return MacroDataSnapshot(
            btc_d=btc_d,
            btc_d_change_24h=None,
            total3_cap=total3,
            total3_change_24h=None,
            timestamp=now,
        )
    except (KeyError, TypeError, ValueError):
        return None


def fetch_macro_data(
    api_key: str = "",
    base_url: str = "https://pro-api.coinmarketcap.com/v1",
    max_age: float = 1800.0,
) -> MacroDataSnapshot:
    global _cached, _last_fetch

    now = time.time()
    if _cached is not None and (now - _last_fetch) < max_age:
        return _cached

    snapshot: MacroDataSnapshot | None = None

    cmc_data = _fetch_cmc(api_key, base_url) if api_key else None
    if cmc_data is not None:
        snapshot = _parse_cmc(cmc_data, api_key, base_url, now)
    else:
        cg_data = _fetch_coingecko()
        if cg_data is not None:
            snapshot = _parse_coingecko(cg_data, now)

    if snapshot is None:
        if _cached is not None:
            return _cached
        return MacroDataSnapshot(timestamp=now)

    _cached = snapshot
    _last_fetch = now
    return snapshot


def clear_macro_cache() -> None:
    global _cached, _last_fetch
    _cached = None
    _last_fetch = 0.0
