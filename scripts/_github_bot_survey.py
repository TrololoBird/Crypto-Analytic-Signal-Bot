"""One-off survey: crypto/futures/signal bots on GitHub (stdout JSON)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

from bot.runtime.errors import DEFENSIVE_EXC

QUERIES = [
    ("popular_futures", "binance futures trading bot language:python", "stars"),
    ("popular_signal", "crypto signal bot telegram binance language:python", "stars"),
    ("popular_algo", "algorithmic trading bot crypto language:python", "stars"),
    ("popular_polars", "polars trading bot language:python", "stars"),
    ("fresh_futures", "binance futures bot language:python", "updated"),
    ("fresh_signal", "telegram trading signal bot language:python", "updated"),
    ("discussed_ft", "freqtrade", "comments"),  # may not work
]

FRAMEWORKS = [
    "freqtrade/freqtrade",
    "jesse-ai/jesse",
    "hummingbot/hummingbot",
    "Drakkar-Software/OctoBot",
    "ccxt/ccxt",
    "sammchardy/python-binance",
    "Erfaniaa/binance-futures-trading-bot",
    "shivpatel-dev/binance-futures-bot",
    "cunarist/solie",
    "Whit1985/Binance-Futures-Signal-Bot",
    "pawelmat142/binance-bot",
    "Janis174756/Binance-Futures-Trading-Bot",
    "Wayy-Research/wrtrade",
    "nikkisora/PolarBT",
    "Yvictor/polars_backtest_extension",
    "zionhann/open-binancian-futures",
    "tiagosiebler/binance",
    "CryptoSignal/Crypto-Signal",
    "ctubio/Krypto-trading-bot",
    "Superalgos/Superalgos",
    "StockSharp/StockSharp",
    "kernc/backtesting.py",
    "mementum/backtrader",
    "vnpy/vnpy",
    "QuantConnect/Lean",
    "blankly-finance/blankly",
    "alpacahq/alpaca-trade-api-python",
    "robcarver17/pysystemtrade",
    "edtechreza/crypto-trading-bot",
    "conor19w/Binance-Futures-Trading-Bot",
]


def api_get(url: str) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "bot2-architecture-survey",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def search_repos(query: str, sort: str, per_page: int = 10) -> list[dict]:
    q = urllib.parse.quote(query)
    url = (
        f"https://api.github.com/search/repositories?q={q}&sort={sort}"
        f"&order=desc&per_page={per_page}"
    )
    try:
        data = api_get(url)
    except urllib.error.HTTPError as exc:
        return [{"error": str(exc), "query": query}]
    items = [
        {
            "full_name": repo["full_name"],
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "updated_at": repo.get("updated_at", ""),
            "open_issues": repo.get("open_issues_count", 0),
            "description": (repo.get("description") or "")[:120],
            "language": repo.get("language"),
            "query_bucket": query,
            "sort": sort,
        }
        for repo in data.get("items", [])
    ]
    time.sleep(2)  # rate limit courtesy
    return items


def count_py_files(full_name: str) -> int | None:
    try:
        url = f"https://api.github.com/repos/{full_name}/git/trees/HEAD?recursive=1"
        data = api_get(url)
        paths = [t["path"] for t in data.get("tree", []) if t.get("path", "").endswith(".py")]
        prod = [
            p
            for p in paths
            if not p.startswith("tests/")
            and "/tests/" not in p
            and "/test/" not in p
            and "test_" not in p.split("/")[-1][:6]
        ]
        return len(prod)
    except DEFENSIVE_EXC:
        return None


def main() -> None:
    seen: OrderedDict[str, dict] = OrderedDict()

    for _label, query, sort in QUERIES:
        for item in search_repos(query, sort, per_page=10):
            if "error" in item:
                continue
            fn = item["full_name"]
            if fn not in seen:
                item["source"] = f"search:{sort}"
                seen[fn] = item

    for fn in FRAMEWORKS:
        if fn in seen:
            continue
        try:
            url = f"https://api.github.com/repos/{fn}"
            repo = api_get(url)
            seen[fn] = {
                "full_name": fn,
                "stars": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "updated_at": repo.get("updated_at", ""),
                "open_issues": repo.get("open_issues_count", 0),
                "description": (repo.get("description") or "")[:120],
                "language": repo.get("language"),
                "source": "curated",
            }
            time.sleep(0.5)
        except DEFENSIVE_EXC:
            pass

    # Top 30 by stars for py count sampling (expensive)
    ranked = sorted(seen.values(), key=lambda x: x.get("stars", 0), reverse=True)
    for item in ranked[:35]:
        fn = item["full_name"]
        item["py_files_approx"] = count_py_files(fn)
        time.sleep(0.3)

    out = {
        "total_unique": len(seen),
        "repos": ranked,
        "by_bucket": {
            label: [i["full_name"] for i in ranked if i.get("query_bucket") == q]
            for label, q, _ in QUERIES
        },
    }
    path = Path(__file__).with_name("_github_bot_survey_out.json")
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(ranked)} repos to {path}")


if __name__ == "__main__":
    main()
