#!/usr/bin/env python3
"""One-shot merge of over-split bot modules (reduces file count for agent navigation)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _strip_module_preamble(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_doc = False
    started = False
    for line in lines:
        if not started:
            if line.startswith(('"""', "'''")):
                in_doc = not in_doc
                continue
            if in_doc:
                continue
            if line.startswith("from __future__"):
                continue
            if not line.strip():
                continue
            if line.startswith(("import ", "from ")):
                continue
            started = True
        out.append(line)
    return "\n".join(out).strip() + "\n"


def _dedupe_import_block(block: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in block.splitlines():
        key = line.strip()
        if key.startswith("from __future__"):
            if key not in seen:
                seen.add(key)
                lines.insert(0, line)
            continue
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


def merge_memory_schema() -> None:
    memory = ROOT / "bot/persistence/repository/memory.py"
    schema = ROOT / "bot/persistence/repository/memory_schema.py"
    if not schema.is_file():
        return
    mem_text = memory.read_text(encoding="utf-8")
    if "MemoryRepositorySchemaMixin" in mem_text and "memory_schema" not in mem_text:
        schema.unlink()
        return
    body = _strip_module_preamble(schema.read_text(encoding="utf-8"))
    mem_text = mem_text.replace(
        "from .memory_schema import MemoryRepositorySchemaMixin\n\n\n",
        "",
    )
    anchor = 'LOG = logging.getLogger("bot.persistence.repository")\n'
    if anchor not in mem_text:
        raise RuntimeError("memory.py anchor missing")
    mem_text = mem_text.replace(
        anchor,
        anchor + "\n" + body + "\n",
        1,
    )
    memory.write_text(mem_text, encoding="utf-8")
    schema.unlink()
    print("merged memory_schema -> memory.py")


def merge_ws_transport() -> None:
    ws = ROOT / "bot/market/ws.py"
    transport = ROOT / "bot/market/ws_transport.py"
    if not transport.is_file():
        return
    ws_text = ws.read_text(encoding="utf-8")
    if "class RateLimiter" in ws_text and "ws_transport" not in ws_text:
        transport.unlink()
        return
    body = _strip_module_preamble(transport.read_text(encoding="utf-8"))
    ws_text = ws_text.replace(
        "from .ws_transport import (\n"
        "    JsonDict,\n"
        "    MessageBuffer,\n"
        "    RateLimiter,\n"
        "    is_global_market_stream,\n"
        ")\n\n",
        "",
    )
    anchor = "if TYPE_CHECKING:\n    from types import ModuleType\n"
    ws_text = ws_text.replace(
        anchor,
        f"# --- ws transport (inlined) ---\n{body}\n\n{anchor}",
    )
    ws.write_text(ws_text, encoding="utf-8")
    transport.unlink()
    print("merged ws_transport -> ws.py")


def merge_ws_submodules() -> None:
    ws_path = ROOT / "bot/market/ws.py"
    order = [
        "ws_enrichment.py",
        "ws_reconnect.py",
        "ws_health.py",
        "ws_subscriptions.py",
        "ws_connection.py",
        "ws_cache.py",
    ]
    if not any((ROOT / "bot/market" / name).is_file() for name in order):
        return
    ws_text = ws_path.read_text(encoding="utf-8")
    ws_text = re.sub(
        r"from \. import ws_cache, ws_connection, ws_health, ws_reconnect, ws_subscriptions\n",
        "",
        ws_text,
    )
    bodies: list[str] = []
    for name in order:
        path = ROOT / "bot/market" / name
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        raw = re.sub(r"from \.ws_enrichment import [^\n]+\n", "", raw)
        bodies.append(f"# --- from {name} ---\n{_strip_module_preamble(raw)}")
        path.unlink()
        print(f"merged {name} -> ws.py")

    block = "\n\n".join(bodies)
    marker = "# --- ws transport (inlined) ---\n"
    if marker in ws_text:
        ws_text = ws_text.replace(marker, marker + block + "\n\n", 1)
    else:
        anchor = "if TYPE_CHECKING:\n    from types import ModuleType\n"
        ws_text = ws_text.replace(anchor, f"{block}\n\n{anchor}")

    for mod in ("ws_cache", "ws_connection", "ws_health", "ws_reconnect", "ws_subscriptions"):
        ws_text = ws_text.replace(f"{mod}.", "")

    ws_path.write_text(ws_text, encoding="utf-8")


def merge_symbol_analyzer() -> None:
    analyzer_dir = ROOT / "bot/runtime/analyzer"
    target = ROOT / "bot/runtime/symbol_analyzer.py"
    pipeline = analyzer_dir / "pipeline.py"
    if not pipeline.is_file():
        return

    parts: list[str] = []
    for name in ("common.py", "_base.py", "family_gates.py"):
        path = analyzer_dir / name
        if path.is_file():
            parts.append(_strip_module_preamble(path.read_text(encoding="utf-8")))
            path.unlink()

    pipe = pipeline.read_text(encoding="utf-8")
    pipe = re.sub(
        r"from bot\.runtime\.analyzer\._base import AnalyzerMixinBase\n",
        "",
        pipe,
    )
    pipe = re.sub(
        r"from bot\.runtime\.analyzer\.family_gates import AnalyzerFamilyGatesMixin\n",
        "",
        pipe,
    )
    pipe = re.sub(
        r"from bot\.runtime\.analyzer\.common import \([^)]+\)\n",
        "",
        pipe,
        flags=re.DOTALL,
    )
    pipe_body = _strip_module_preamble(pipe)
    parts.append(pipe_body)
    pipeline.unlink()

    init = analyzer_dir / "__init__.py"
    if init.is_file():
        init.unlink()

    import_lines: list[str] = []
    for line in pipe.splitlines():
        if line.startswith(("import ", "from ")) or line.strip() == "":
            if line.startswith("from bot.runtime.analyzer"):
                continue
            import_lines.append(line)
        else:
            break
    imports = _dedupe_import_block("\n".join(import_lines))

    out = (
        '"""Symbol analysis: frames → prepare → engine → filters (single module)."""\n\n'
        f"{imports}\n\n"
        + "\n\n".join(parts)
        + "\n\n\nclass SymbolAnalyzer(AnalyzerPipelineMixin):\n"
        '    """Per-symbol pipeline entry."""\n'
    )
    target.write_text(out, encoding="utf-8")
    if analyzer_dir.is_dir() and not any(analyzer_dir.iterdir()):
        analyzer_dir.rmdir()
    print("merged analyzer/ -> symbol_analyzer.py")


def merge_telemetry_analysis() -> None:
    src = ROOT / "bot/diagnostics/telemetry_strategy_analysis.py"
    dst = ROOT / "bot/diagnostics/runtime_analysis.py"
    if not src.is_file():
        return
    dst_text = dst.read_text(encoding="utf-8")
    if "def analyze_telemetry" in dst_text:
        src.unlink()
        return
    body = _strip_module_preamble(src.read_text(encoding="utf-8"))
    if "from bot.domain.config import _ALL_SETUP_IDS" not in dst_text:
        dst_text = dst_text.replace(
            "from typing import TYPE_CHECKING, Any\n",
            "from typing import TYPE_CHECKING, Any\n\nfrom bot.domain.config import _ALL_SETUP_IDS\n",
        )
    dst_text = dst_text.rstrip() + "\n\n\n# --- telemetry strategy analysis (inlined) ---\n\n" + body + "\n"
    dst.write_text(dst_text, encoding="utf-8")
    src.unlink()
    print("merged telemetry_strategy_analysis -> runtime_analysis.py")


def merge_live_watch() -> None:
    src = ROOT / "bot/diagnostics/live_watch.py"
    dst = ROOT / "bot/diagnostics/runtime_analysis.py"
    if not src.is_file():
        return
    body = src.read_text(encoding="utf-8")
    body = body.replace(
        "from bot.diagnostics.runtime_analysis import (\n"
        "    file_has_rows,\n"
        "    find_latest_run_dir,\n"
        "    parse_strategy_decision_log_lines,\n"
        "    read_jsonl,\n"
        ")\n"
        "from bot.diagnostics.telemetry_strategy_analysis import analyze_decision_rows, analyze_telemetry\n",
        "",
    )
    body = _strip_module_preamble(body)
    dst_text = dst.read_text(encoding="utf-8")
    if "def find_live_watch_session" in dst_text:
        src.unlink()
        return
    dst_text = dst_text.rstrip() + "\n\n\n# --- live_watch (inlined) ---\n\n" + body + "\n"
    dst.write_text(dst_text, encoding="utf-8")
    src.unlink()
    print("merged live_watch -> runtime_analysis.py")


def patch_imports() -> None:
    replacements = [
        ("bot.diagnostics.telemetry_strategy_analysis", "bot.diagnostics.runtime_analysis"),
        ("bot.diagnostics.live_watch", "bot.diagnostics.runtime_analysis"),
        ("bot.runtime.analyzer.pipeline", "bot.runtime.symbol_analyzer"),
        ("bot.runtime.analyzer.family_gates", "bot.runtime.symbol_analyzer"),
        ("bot.market.ws_enrichment", "bot.market.ws"),
        ("bot.market.ws_cache", "bot.market.ws"),
    ]
    for root in (ROOT / "bot", ROOT / "scripts", ROOT / "tests"):
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if py.name == "consolidate_bot_modules.py":
                continue
            text = py.read_text(encoding="utf-8")
            new = text
            for old, new_mod in replacements:
                new = new.replace(old, new_mod)
            if new != text:
                py.write_text(new, encoding="utf-8")
    print("patched imports")


def main() -> None:
    """Safe merges only. WS/diagnostics/memory merges removed — they broke imports."""
    merge_symbol_analyzer()
    # merge_memory_schema()  # use git-inline memory.py instead
    # merge_ws_* — keep ws_*.py modules (7 files) until a tested inliner exists
    patch_imports()
    count = len(list((ROOT / "bot").rglob("*.py")))
    print(f"bot/*.py count: {count}")


if __name__ == "__main__":
    main()
