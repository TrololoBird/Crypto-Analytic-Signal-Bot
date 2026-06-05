#!/usr/bin/env python3
"""Merge over-split bot modules into parent files (file-count reduction)."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"


def _skip_docstring(lines: list[str], start: int) -> int:
    i = start
    if i >= len(lines):
        return i
    s = lines[i].strip()
    if s.startswith('"""') or s.startswith("'''"):
        quote = s[:3]
        if s.count(quote) >= 2 and len(s) > 6:
            return i + 1
        i += 1
        while i < len(lines):
            if quote in lines[i]:
                return i + 1
            i += 1
    return i


def split_imports_and_body(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    i = _skip_docstring(lines, 0)
    if i < len(lines) and lines[i].strip().startswith("from __future__"):
        i += 1
    imports: list[str] = []
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        if s.startswith(("import ", "from ")):
            imports.append(line)
            i += 1
            continue
        break
    return imports, lines[i:]


def dedupe_imports(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def merge_ws() -> None:
    ws_path = BOT / "market" / "ws.py"
    modules = [
        "ws_enrichment.py",
        "ws_reconnect.py",
        "ws_health.py",
        "ws_subscriptions.py",
        "ws_connection.py",
        "ws_cache.py",
    ]
    skip_defs = {
        "class RateLimiter",
        "class MessageBuffer",
        "def is_global_market_stream",
    }
    skip_assigns = {"LOG =", "JsonDict =", "KlineCloseCallback =", "AggTradeCallback =", "ReconnectCallback ="}
    extra_imports: list[str] = []
    chunks: list[str] = []
    for name in modules:
        path = BOT / "market" / name
        if not path.is_file():
            continue
        imp, body = split_imports_and_body(path.read_text(encoding="utf-8"))
        for line in imp:
            if "ws_enrichment" in line or "ws_cache" in line or "ws_connection" in line:
                continue
            extra_imports.append(line)
        filtered: list[str] = []
        for line in body:
            if any(line.startswith(p) for p in skip_defs):
                continue
            if any(line.startswith(p) for p in skip_assigns):
                continue
            if line.startswith("from .ws_"):
                continue
            filtered.append(line)
        chunks.append(f"\n# --- inlined from {name} ---\n" + "\n".join(filtered).strip())
        path.unlink()
        print(f"ws: removed {name}")

    text = ws_path.read_text(encoding="utf-8")
    text = re.sub(
        r"from \. import ws_cache, ws_connection, ws_health, ws_reconnect, ws_subscriptions\n",
        "",
        text,
    )
    for mod in ("ws_cache", "ws_connection", "ws_health", "ws_reconnect", "ws_subscriptions"):
        text = text.replace(f"{mod}.", "")

    header, _, rest = text.partition("class FuturesWSManager:")
    imp, main_body = split_imports_and_body(header)
    all_imports = dedupe_imports(imp + extra_imports)
    block = "\n".join(chunks)
    new_header = "\n".join(all_imports) + "\n\n" + "\n".join(main_body).strip() + "\n\n" + block + "\n\n"
    ws_path.write_text(new_header + "class FuturesWSManager:" + rest, encoding="utf-8")
    print("ws: merged into ws.py")


def merge_rest() -> None:
    impl = BOT / "market" / "rest_impl.py"
    parts = ["rest_validators.py", "rest_frames.py", "rest_abc.py", "rest_http.py"]
    extra_imports: list[str] = []
    chunks: list[str] = []
    for name in parts:
        path = BOT / "market" / name
        if not path.is_file():
            continue
        imp, body = split_imports_and_body(path.read_text(encoding="utf-8"))
        for line in imp:
            if "rest_impl" in line or "rest_http" in line or "rest_frames" in line or "rest_validators" in line:
                continue
            if "rest_abc" in line and name != "rest_abc.py":
                continue
            extra_imports.append(line)
        filtered = [
            line
            for line in body
            if not line.startswith("from bot.market.rest_") and not line.startswith("from .rest_")
        ]
        chunks.append(f"\n# --- inlined from {name} ---\n" + "\n".join(filtered).strip())
        path.unlink()
        print(f"rest: removed {name}")

    text = impl.read_text(encoding="utf-8")
    for old in (
        "from bot.market.rest_abc import BinanceClient\n",
        "from bot.market.rest_frames import (\n",
        "from bot.market.rest_http import RestHttpMixin\n",
        "from bot.market.rest_validators import (\n",
    ):
        text = text.replace(old, "")
    # Drop multiline imports from rest_frames/rest_validators
    text = re.sub(r"from bot\.market\.rest_frames import \([^)]+\)\n", "", text, flags=re.DOTALL)
    text = re.sub(r"from bot\.market\.rest_validators import \([^)]+\)\n", "", text, flags=re.DOTALL)

    imp, body = split_imports_and_body(text)
    all_imports = dedupe_imports(imp + extra_imports)
    impl.write_text("\n".join(all_imports) + "\n\n" + "\n".join(chunks) + "\n\n" + "\n".join(body) + "\n", encoding="utf-8")

    rest = BOT / "market" / "rest.py"
    rest.write_text(
        textwrap.dedent(
            '''\
            """Public Binance REST client facade (implementation in rest_impl)."""
            from __future__ import annotations

            from bot.market.rest_impl import (
                BinanceClient,
                BinanceClientImpl,
                RestHttpMixin,
            )

            __all__ = ["BinanceClient", "BinanceClientImpl", "RestHttpMixin"]
            '''
        ),
        encoding="utf-8",
    )
    print("rest: merged into rest_impl.py")


def merge_diagnostics_analyzer() -> None:
    target = BOT / "diagnostics" / "analyzer_ops.py"
    parts = ["metrics.py", "tracker.py", "reporter.py"]
    chunks: list[str] = []
    imports: list[str] = []
    for name in parts:
        path = BOT / "diagnostics" / "analyzer" / name
        if not path.is_file():
            continue
        imp, body = split_imports_and_body(path.read_text(encoding="utf-8"))
        imports.extend(imp)
        filtered = [line for line in body if not line.startswith("from ..analyzer")]
        chunks.append(f"\n# --- from analyzer/{name} ---\n" + "\n".join(filtered).strip())
        path.unlink()
    (BOT / "diagnostics" / "analyzer" / "__init__.py").unlink(missing_ok=True)
    try:
        (BOT / "diagnostics" / "analyzer").rmdir()
    except OSError:
        pass
    target.write_text(
        '"""Diagnostics analyzer helpers (metrics, tracker, reporter)."""\n\n'
        + "\n".join(dedupe_imports(imports))
        + "\n"
        + "\n".join(chunks)
        + "\n",
        encoding="utf-8",
    )
    print("diagnostics: analyzer_ops.py")


def merge_diagnostics_runtime() -> None:
    target = BOT / "diagnostics" / "runtime_ops.py"
    parts = ["alerts.py", "metrics.py", "health.py", "strategy_audit.py"]
    chunks: list[str] = []
    imports: list[str] = []
    for name in parts:
        path = BOT / "diagnostics" / "runtime" / name
        if not path.is_file():
            continue
        imp, body = split_imports_and_body(path.read_text(encoding="utf-8"))
        imports.extend(imp)
        filtered = []
        for line in body:
            if line.strip().startswith("from ..analyzer.metrics"):
                filtered.append(
                    "    from bot.diagnostics.analyzer_ops import PerformanceMetrics, WinRateCalculator"
                )
                continue
            if line.startswith("from .") or line.startswith("from ..runtime"):
                continue
            filtered.append(line)
        chunks.append(f"\n# --- from runtime/{name} ---\n" + "\n".join(filtered).strip())
        path.unlink()

    target.write_text(
        '"""Runtime health, metrics, alerts, strategy audit (single module)."""\n\n'
        + "\n".join(dedupe_imports(imports))
        + "\n"
        + "\n".join(chunks)
        + "\n",
        encoding="utf-8",
    )

    init = BOT / "diagnostics" / "__init__.py"
    init_text = init.read_text(encoding="utf-8")
    init_text = init_text.replace('".runtime"', '".runtime_ops"')
    init.write_text(init_text, encoding="utf-8")
    (BOT / "diagnostics" / "runtime" / "__init__.py").unlink(missing_ok=True)
    try:
        (BOT / "diagnostics" / "runtime").rmdir()
    except OSError:
        pass
    print("diagnostics: runtime_ops.py")


def merge_diagnostics_session() -> None:
    target = BOT / "diagnostics" / "session_ops.py"
    order = ["telemetry_strategy_analysis.py", "runtime_analysis.py", "live_watch.py"]
    chunks: list[str] = []
    imports: list[str] = []
    for name in order:
        path = BOT / "diagnostics" / name
        if not path.is_file():
            continue
        imp, body = split_imports_and_body(path.read_text(encoding="utf-8"))
        for line in imp:
            if "runtime_analysis" in line or "telemetry_strategy" in line or "live_watch" in line:
                continue
            imports.append(line)
        filtered = []
        for line in body:
            if line.startswith("from bot.diagnostics.runtime_analysis"):
                continue
            if line.startswith("from bot.diagnostics.telemetry_strategy"):
                continue
            filtered.append(line)
        chunks.append(f"\n# --- from {name} ---\n" + "\n".join(filtered).strip())
        path.unlink()
        print(f"session: removed {name}")

    target.write_text(
        '"""Session telemetry, live_watch bridge, runtime log analysis."""\n\n'
        + "\n".join(dedupe_imports(imports))
        + "\n"
        + "\n".join(chunks)
        + "\n",
        encoding="utf-8",
    )
    print("diagnostics: session_ops.py")


def patch_imports() -> None:
    replacements = [
        ("bot.market.ws_cache", "bot.market.ws"),
        ("bot.market.ws_enrichment", "bot.market.ws"),
        ("bot.market.ws_connection", "bot.market.ws"),
        ("bot.market.ws_health", "bot.market.ws"),
        ("bot.market.ws_reconnect", "bot.market.ws"),
        ("bot.market.ws_subscriptions", "bot.market.ws"),
        ("bot.market.rest_abc", "bot.market.rest_impl"),
        ("bot.market.rest_frames", "bot.market.rest_impl"),
        ("bot.market.rest_http", "bot.market.rest_impl"),
        ("bot.market.rest_validators", "bot.market.rest_impl"),
        ("from ..market.ws_enrichment", "from ..market.ws"),
        ("from .ws_enrichment", "from .ws"),
        ("bot.diagnostics.runtime.health", "bot.diagnostics.runtime_ops"),
        ("bot.diagnostics.runtime.alerts", "bot.diagnostics.runtime_ops"),
        ("bot.diagnostics.runtime.metrics", "bot.diagnostics.runtime_ops"),
        ("bot.diagnostics.runtime.strategy_audit", "bot.diagnostics.runtime_ops"),
        ("bot.diagnostics.runtime_analysis", "bot.diagnostics.session_ops"),
        ("bot.diagnostics.live_watch", "bot.diagnostics.session_ops"),
        ("bot.diagnostics.telemetry_strategy_analysis", "bot.diagnostics.session_ops"),
        ("from ..analyzer.metrics", "from bot.diagnostics.analyzer_ops"),
    ]
    for root in (BOT, ROOT / "scripts", ROOT / "tests"):
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if py.name in {"consolidate_all_modules.py", "consolidate_bot_modules.py"}:
                continue
            text = py.read_text(encoding="utf-8")
            new = text
            for old, new_mod in replacements:
                new = new.replace(old, new_mod)
            if new != text:
                py.write_text(new, encoding="utf-8")
    print("imports patched")


def merge_outcomes_query() -> None:
    mem = BOT / "persistence" / "repository" / "memory.py"
    q = BOT / "persistence" / "repository" / "queries" / "outcomes.py"
    if not q.is_file():
        return
    imp, body = split_imports_and_body(q.read_text(encoding="utf-8"))
    text = mem.read_text(encoding="utf-8")
    text = text.replace("from .queries.outcomes import fetch_setup_stats_rows, fetch_signal_outcome_rows\n", "")
    anchor = 'LOG = logging.getLogger("bot.persistence.repository")\n'
    block = "\n".join(imp) + "\n\n" + "\n".join(body).strip() + "\n\n"
    text = text.replace(anchor, anchor + "\n" + block, 1)
    mem.write_text(text, encoding="utf-8")
    q.unlink()
    (BOT / "persistence" / "repository" / "queries" / "__init__.py").unlink(missing_ok=True)
    try:
        (BOT / "persistence" / "repository" / "queries").rmdir()
    except OSError:
        pass
    print("persistence: outcomes query inlined into memory.py")


def main() -> None:
    merge_ws()
    merge_rest()
    merge_diagnostics_analyzer()
    merge_diagnostics_runtime()
    merge_diagnostics_session()
    merge_outcomes_query()
    patch_imports()
    count = len(list(BOT.rglob("*.py")))
    print(f"bot/*.py count: {count}")


if __name__ == "__main__":
    main()
