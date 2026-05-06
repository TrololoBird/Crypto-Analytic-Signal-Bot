from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sqlite3
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data" / "bot"
TELEMETRY_DIR = DATA_DIR / "telemetry"

TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".csv",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".jsonl",
    ".lock",
    ".log",
    ".md",
    ".py",
    ".ps1",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".env", ".ini", ".cfg"}
CONFIG_NAMES = {
    "docker-compose.yml",
    "config.toml.example",
    "env.example",
    "Makefile",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
}
SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|telegram|chat[_-]?id|bot[_-]?token)",
    re.IGNORECASE,
)
TIME_KEY_RE = re.compile(r"(time|timestamp|date|created|updated|opened|closed|detected)", re.IGNORECASE)
LOG_TIME_RE = re.compile(
    r"(?P<ts>20\d\d[-/.]\d\d[-/.]\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?)"
)
ERROR_RE = re.compile(
    r"\b(ERROR|CRITICAL|FATAL|Traceback|Exception|failed|failure)\b",
    re.IGNORECASE,
)
WARNING_RE = re.compile(r"\b(WARN|WARNING)\b", re.IGNORECASE)
DISCONNECT_RE = re.compile(r"\b(disconnect|reconnect|websocket|ws_|connection reset|timeout)\b", re.IGNORECASE)
RATE_LIMIT_RE = re.compile(r"\b(rate limit|429|418|too many requests)\b", re.IGNORECASE)
REJECT_RE = re.compile(r"\b(reject|rejected|rejection)\b", re.IGNORECASE)
SIGNAL_RE = re.compile(r"\b(signal|candidate|selected|delivered|telegram)\b", re.IGNORECASE)


@dataclass(frozen=True)
class FileInfo:
    path: Path
    rel: str
    size: int
    mtime_utc: str
    suffix: str
    category: str
    sha256: str
    line_count: int | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def markdown_link(path: Path) -> str:
    return rel(path)


def redact_value(key: str, value: Any) -> Any:
    if SECRET_RE.search(key):
        text = "" if value is None else str(value)
        if not text:
            return "<empty>"
        return f"<redacted:{len(text)} chars>"
    if isinstance(value, dict):
        return {str(k): redact_value(f"{key}.{k}", v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, item) for item in value]
    return value


def redact_text(text: str) -> str:
    text = re.sub(
        r"(?i)(api[_-]?key|secret|token|password|bot[_-]?token)\s*[:=]\s*['\"]?[^'\"\s,}]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?i)(chat[_-]?id)\s*[:=]\s*['\"]?[-\w]+", r"\1=<redacted>", text)
    return text


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, inner in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(inner, next_prefix))
    elif isinstance(value, list):
        out[prefix] = value
    else:
        out[prefix] = value
    return out


def purpose_for_key(path: str, key: str) -> str:
    key_l = f"{path}.{key}".lower()
    if "telegram" in key_l:
        return "Telegram delivery/tracking integration"
    if "dashboard" in key_l or "api" in key_l:
        return "Dashboard or HTTP API runtime"
    if "setups" in key_l or "strategy" in key_l or "filters" in key_l:
        return "Strategy enablement, thresholds, or filtering"
    if "risk" in key_l or "stop" in key_l or "take_profit" in key_l:
        return "Signal risk and exit parameter"
    if "market" in key_l or "binance" in key_l or "websocket" in key_l or "ws" in key_l:
        return "Public market-data acquisition"
    if "telemetry" in key_l or "log" in key_l:
        return "Persistence, logging, or telemetry output"
    if "ml" in key_l or "model" in key_l or "train" in key_l:
        return "ML or analytics configuration"
    if "test" in key_l or "pytest" in key_l or "ruff" in key_l:
        return "Developer validation tooling"
    return "Purpose inferred only from name/path; verify against code consumers before changing"


def iter_files(include_git: bool = False) -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if path.is_dir():
            continue
        if not include_git and ".git" in path.parts:
            continue
        files.append(path)
    return sorted(files, key=lambda p: rel(p).lower())


def categorize(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    suffix = path.suffix.lower()
    if ".git" in parts:
        return "vcs-metadata"
    if "__pycache__" in parts or name.endswith(".pyc"):
        return "generated-cache"
    if ".pytest_cache" in parts or ".ruff_cache" in parts or "crypto_signal_bot.egg-info" in parts:
        return "generated-cache"
    if "data" in parts and "telemetry" in parts:
        return "telemetry"
    if "data" in parts and suffix in {".db", ".sqlite", ".sqlite3"}:
        return "database"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "database"
    if suffix == ".log" or name.endswith(".stdout.log") or name.endswith(".stderr.log"):
        return "log"
    if suffix in CONFIG_SUFFIXES or name in {n.lower() for n in CONFIG_NAMES}:
        return "config"
    if suffix == ".py" and "bot" in parts:
        return "source-runtime"
    if suffix == ".py" and "scripts" in parts:
        return "source-script"
    if suffix == ".py" and "tests" in parts:
        return "source-test"
    if suffix == ".py":
        return "source-other"
    if suffix in {".md", ".rst"}:
        return "docs"
    return "other"


def file_sha256_and_lines(path: Path) -> tuple[str, int | None]:
    hasher = hashlib.sha256()
    line_count = 0
    text_candidate = path.suffix.lower() in TEXT_SUFFIXES
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
                if text_candidate:
                    line_count += chunk.count(b"\n")
    except OSError as exc:
        return f"<read-error:{exc}>", None
    return hasher.hexdigest(), line_count if text_candidate else None


def collect_file_info(include_git: bool = False) -> list[FileInfo]:
    infos: list[FileInfo] = []
    for path in iter_files(include_git=include_git):
        try:
            stat = path.stat()
        except OSError:
            continue
        sha, lines = file_sha256_and_lines(path)
        infos.append(
            FileInfo(
                path=path,
                rel=rel(path),
                size=stat.st_size,
                mtime_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
                    timespec="seconds"
                ),
                suffix=path.suffix.lower(),
                category=categorize(path),
                sha256=sha,
                line_count=lines,
            )
        )
    return infos


def parse_imports(path: Path) -> set[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.add(module)
    return imports


def module_name(path: Path) -> str:
    try:
        rel_path = path.relative_to(ROOT)
    except ValueError:
        return path.stem
    parts = list(rel_path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def build_import_graph(files: list[FileInfo]) -> tuple[list[tuple[str, str]], set[str], set[str]]:
    py_paths = [info.path for info in files if info.suffix == ".py"]
    project_modules = {module_name(path) for path in py_paths}
    edges: list[tuple[str, str]] = []
    incoming: Counter[str] = Counter()
    for path in py_paths:
        src = module_name(path)
        for raw in parse_imports(path):
            normalized = raw.lstrip(".")
            if not normalized:
                continue
            target = ""
            for mod in sorted(project_modules, key=len, reverse=True):
                if normalized == mod or normalized.startswith(mod + "."):
                    target = mod
                    break
            if target:
                edges.append((src, target))
                incoming[target] += 1
    entrypoint_like = {
        module_name(path)
        for path in py_paths
        if path.name in {"main.py", "run_check.py", "monitor_bot.py", "test_bot.py"}
        or "scripts" in path.parts
        or "tests" in path.parts
    }
    orphan_candidates = {
        mod
        for mod in project_modules
        if incoming[mod] == 0
        and mod not in entrypoint_like
        and not mod.endswith(".__main__")
    }
    return sorted(edges), project_modules, orphan_candidates


def parse_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name.endswith(".toml.example"):
        suffix = ".toml"
    elif name.endswith(".json.example"):
        suffix = ".json"
    elif name.endswith(".env.example"):
        suffix = ".env"
    try:
        if suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return flatten(redact_value(rel(path), data)), warnings
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            return flatten(redact_value(rel(path), data)), warnings
        if suffix == ".env":
            rows: dict[str, Any] = {}
            for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    warnings.append(f"line {idx}: not KEY=VALUE")
                    continue
                key, value = stripped.split("=", 1)
                rows[key.strip()] = redact_value(key.strip(), value.strip())
            return rows, warnings
        if suffix in {".yaml", ".yml"}:
            rows = {}
            for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", line.strip())
                if match:
                    rows[f"line_{idx}.{match.group(1)}"] = redact_value(
                        match.group(1), match.group(2)
                    )
            warnings.append("YAML parsed with a shallow key scanner, not a full YAML parser")
            return rows, warnings
        if path.name.lower() == "requirements.txt":
            rows = {
                f"requirement_{idx}": line.strip()
                for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if line.strip() and not line.strip().startswith("#")
            }
            return rows, warnings
        if path.name == "Makefile" or suffix in {".ini", ".cfg", ".lock"}:
            rows = {}
            for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    rows[f"line_{idx}"] = redact_text(stripped[:240])
            return rows, warnings
    except Exception as exc:
        warnings.append(f"parse error: {exc}")
    return {}, warnings


def table_markdown(rows: Iterable[Iterable[Any]]) -> str:
    rendered = []
    for row in rows:
        rendered.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return "\n".join(rendered)


def write_file_map(files: list[FileInfo]) -> None:
    edges, modules, orphan_candidates = build_import_graph(files)
    category_counts = Counter(info.category for info in files)
    suffix_counts = Counter(info.suffix or "<none>" for info in files)
    top_large = sorted(files, key=lambda info: info.size, reverse=True)[:30]
    entrypoints = [
        info
        for info in files
        if info.rel in {"main.py", "run_check.py", "monitor_bot.py", "test_bot.py"}
        or info.rel.startswith("scripts/live_")
        or info.rel == "run_30min_test.bat"
        or info.rel == "Makefile"
    ]

    lines = [
        "# Phase 0.1 File System Audit",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: every non-.git file under the repository root was enumerated, hashed, categorized, and line-counted when text-like.",
        "- Confirmed: .git object internals were excluded from content hashing because they are VCS storage, not project runtime input.",
        "- Inference: orphan candidates are based on static Python imports only; dynamic imports, CLI entry points, and operator-only scripts can make these false positives.",
        "",
        "## Summary",
        "",
        f"- Files scanned: {len(files)}",
        f"- Total bytes: {sum(info.size for info in files):,}",
        "",
        "### By Category",
        "",
        "| Category | Files | Bytes |",
        "| --- | ---: | ---: |",
    ]
    for category, count in category_counts.most_common():
        size = sum(info.size for info in files if info.category == category)
        lines.append(f"| {category} | {count} | {size:,} |")

    lines.extend(["", "### By Suffix", "", "| Suffix | Files |", "| --- | ---: |"])
    for suffix, count in suffix_counts.most_common():
        lines.append(f"| {suffix} | {count} |")

    lines.extend(["", "## Entry Points", "", "| Path | Category | Size |", "| --- | --- | ---: |"])
    for info in sorted(entrypoints, key=lambda item: item.rel):
        lines.append(f"| {markdown_link(info.path)} | {info.category} | {info.size:,} |")

    lines.extend(["", "## Import Dependency Graph", ""])
    lines.append(f"- Project Python modules detected: {len(modules)}")
    lines.append(f"- Static intra-project import edges: {len(edges)}")
    lines.append("")
    lines.append("| Source | Target |")
    lines.append("| --- | --- |")
    for src, target in edges[:500]:
        lines.append(f"| {src} | {target} |")
    if len(edges) > 500:
        lines.append(f"| ... | {len(edges) - 500} more edges omitted from display |")

    lines.extend(["", "## Potentially Unused Or Orphan Python Modules", ""])
    if orphan_candidates:
        lines.append("| Module |")
        lines.append("| --- |")
        for mod in sorted(orphan_candidates):
            lines.append(f"| {mod} |")
    else:
        lines.append("No static orphan candidates found.")

    lines.extend(["", "## Largest Files", "", "| Path | Category | Bytes | Lines | SHA256 |", "| --- | --- | ---: | ---: | --- |"])
    for info in top_large:
        lines.append(
            f"| {markdown_link(info.path)} | {info.category} | {info.size:,} | {info.line_count if info.line_count is not None else ''} | `{info.sha256[:16]}...` |"
        )

    lines.extend(["", "## Full File Manifest", "", "| Path | Category | Bytes | Modified UTC | Lines | SHA256 |", "| --- | --- | ---: | --- | ---: | --- |"])
    for info in files:
        lines.append(
            f"| {markdown_link(info.path)} | {info.category} | {info.size:,} | {info.mtime_utc} | {info.line_count if info.line_count is not None else ''} | `{info.sha256}` |"
        )

    (LOG_DIR / "00_file_map.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_config_audit(files: list[FileInfo]) -> None:
    config_infos = [
        info
        for info in files
        if info.category == "config"
        or info.path.name in CONFIG_NAMES
        or info.path.suffix.lower() in CONFIG_SUFFIXES
    ]
    parsed: dict[str, dict[str, Any]] = {}
    warnings_by_file: dict[str, list[str]] = {}
    key_sources: defaultdict[str, list[str]] = defaultdict(list)

    for info in config_infos:
        values, warnings = parse_config(info.path)
        parsed[info.rel] = values
        warnings_by_file[info.rel] = warnings
        for key in values:
            key_sources[key].append(info.rel)

    current = parsed.get("config.toml", {})
    example = parsed.get("config.toml.example", {})
    missing_in_example = sorted(set(current) - set(example))
    missing_in_current = sorted(set(example) - set(current))
    shared_changed = sorted(
        key for key in set(current) & set(example) if str(current[key]) != str(example[key])
    )

    duplicate_keys = {
        key: sources for key, sources in key_sources.items() if len(sources) > 1 and not key.startswith("line_")
    }

    lines = [
        "# Phase 0.2 Configuration Audit",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: config-like files were read from the filesystem and parsed with stdlib TOML/JSON/env parsing where applicable.",
        "- Confirmed: secret-like values are redacted in this report.",
        "- Uncertainty: YAML files are scanned shallowly unless a full YAML parser is added; treat nested YAML values as incomplete.",
        "- Inference: parameter purposes are inferred from key/path names and must be verified against runtime consumers before calibration.",
        "",
        "## Files",
        "",
        "| Path | Values Parsed | Warnings |",
        "| --- | ---: | --- |",
    ]
    for info in config_infos:
        warnings = "; ".join(warnings_by_file.get(info.rel, []))
        lines.append(f"| {markdown_link(info.path)} | {len(parsed.get(info.rel, {}))} | {warnings} |")

    lines.extend(["", "## config.toml vs config.toml.example", ""])
    lines.append(f"- Keys present in config.toml but not example: {len(missing_in_example)}")
    lines.append(f"- Keys present in example but not config.toml: {len(missing_in_current)}")
    lines.append(f"- Shared keys with different values: {len(shared_changed)}")

    for title, keys in [
        ("Keys Missing From Example", missing_in_example),
        ("Keys Missing From Current Config", missing_in_current),
        ("Shared Keys With Different Values", shared_changed),
    ]:
        lines.extend(["", f"### {title}", ""])
        if not keys:
            lines.append("None.")
            continue
        lines.append("| Key | Current | Example | Purpose |")
        lines.append("| --- | --- | --- | --- |")
        for key in keys:
            cur = redact_value(key, current.get(key, ""))
            ex = redact_value(key, example.get(key, ""))
            lines.append(
                f"| `{key}` | `{json.dumps(cur, default=str)[:180]}` | `{json.dumps(ex, default=str)[:180]}` | {purpose_for_key('config.toml', key)} |"
            )

    lines.extend(["", "## Duplicate Parameter Keys Across Config Files", ""])
    if duplicate_keys:
        lines.append("| Key | Sources |")
        lines.append("| --- | --- |")
        for key, sources in sorted(duplicate_keys.items()):
            lines.append(f"| `{key}` | {', '.join(sources)} |")
    else:
        lines.append("No duplicate flattened keys found across parsed configs.")

    lines.extend(["", "## Parameter Inventory", ""])
    for path, values in sorted(parsed.items()):
        lines.extend(["", f"### {path}", ""])
        if not values:
            lines.append("No parseable values.")
            continue
        lines.append("| Key | Value | Purpose |")
        lines.append("| --- | --- | --- |")
        for key, value in sorted(values.items()):
            safe = redact_value(key, value)
            rendered = json.dumps(safe, default=str, ensure_ascii=False)
            if len(rendered) > 240:
                rendered = rendered[:237] + "..."
            lines.append(f"| `{key}` | `{rendered}` | {purpose_for_key(path, key)} |")

    (LOG_DIR / "01_config_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def sqlite_connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def try_scalar(cursor: sqlite3.Cursor, sql: str) -> Any:
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as exc:
        return f"<query-error:{exc}>"


def normalize_time_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000
        if value > 1_000_000_000:
            try:
                return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
            except Exception:
                return str(value)
    return str(value)


def write_db_audit(files: list[FileInfo]) -> None:
    db_infos = [info for info in files if info.category == "database"]
    lines = [
        "# Phase 0.3 Database Audit",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: SQLite databases were opened read-only.",
        "- Confirmed: schema, indexes, integrity checks, row counts, timestamp ranges, null counts, and canonical row digests were queried.",
        "- Uncertainty: application-level meaning of stale records depends on code contracts and is not inferred solely from schema.",
        "",
    ]

    if not db_infos:
        lines.append("No SQLite database files found.")
        (LOG_DIR / "02_db_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    for info in db_infos:
        lines.extend(["", f"## {info.rel}", ""])
        try:
            conn = sqlite_connect_readonly(info.path)
            cur = conn.cursor()
            integrity = try_scalar(cur, "PRAGMA integrity_check")
            page_count = try_scalar(cur, "PRAGMA page_count")
            page_size = try_scalar(cur, "PRAGMA page_size")
            user_version = try_scalar(cur, "PRAGMA user_version")
            lines.append(f"- Size bytes: {info.size:,}")
            lines.append(f"- SHA256: `{info.sha256}`")
            lines.append(f"- integrity_check: `{integrity}`")
            lines.append(f"- page_count: `{page_count}`")
            lines.append(f"- page_size: `{page_size}`")
            lines.append(f"- user_version: `{user_version}`")

            cur.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','index','view','trigger') ORDER BY type, name"
            )
            objects = cur.fetchall()
            lines.extend(["", "### Schema Objects", "", "| Type | Name | SQL |", "| --- | --- | --- |"])
            for name, obj_type, sql in [(row[0], row[1], row[2]) for row in objects]:
                sql_rendered = (sql or "").replace("\n", " ")
                if len(sql_rendered) > 260:
                    sql_rendered = sql_rendered[:257] + "..."
                lines.append(f"| {obj_type} | `{name}` | `{sql_rendered}` |")

            tables = [row[0] for row in objects if row[1] == "table" and not row[0].startswith("sqlite_")]
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                row_count = try_scalar(cur, f"SELECT COUNT(*) FROM {quoted}")
                lines.extend(["", f"### Table `{table}`", ""])
                lines.append(f"- Rows: {row_count}")

                try:
                    cur.execute(f"SELECT * FROM {quoted}")
                    row_cols = [desc[0] for desc in cur.description or []]
                    digest = hashlib.sha256()
                    scanned_rows = 0
                    canonical_bytes = 0
                    for row in cur:
                        row_obj = {
                            str(key): row[idx]
                            for idx, key in enumerate(row_cols)
                        }
                        payload = json.dumps(
                            row_obj,
                            default=str,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8", errors="replace")
                        digest.update(payload)
                        digest.update(b"\n")
                        canonical_bytes += len(payload)
                        scanned_rows += 1
                    lines.append(
                        f"- Row scan: read {scanned_rows} rows; canonical_json_bytes={canonical_bytes:,}; row_sha256=`{digest.hexdigest()}`"
                    )
                except Exception as exc:
                    lines.append(f"- Row scan error: `{exc}`")

                cur.execute(f"PRAGMA table_info({quoted})")
                cols = cur.fetchall()
                lines.extend(["", "| Column | Type | Not Null | Default | PK | Nulls | Distinct | Min Time | Max Time |", "| --- | --- | ---: | --- | ---: | ---: | ---: | --- | --- |"])
                for cid, col_name, col_type, not_null, default, pk in cols:
                    q_col = '"' + str(col_name).replace('"', '""') + '"'
                    nulls = try_scalar(cur, f"SELECT COUNT(*) FROM {quoted} WHERE {q_col} IS NULL")
                    distinct = try_scalar(cur, f"SELECT COUNT(DISTINCT {q_col}) FROM {quoted}")
                    min_t = max_t = ""
                    if TIME_KEY_RE.search(str(col_name)):
                        min_raw = try_scalar(cur, f"SELECT MIN({q_col}) FROM {quoted}")
                        max_raw = try_scalar(cur, f"SELECT MAX({q_col}) FROM {quoted}")
                        min_t = normalize_time_value(min_raw)
                        max_t = normalize_time_value(max_raw)
                    default_rendered = "" if default is None else str(default)
                    lines.append(
                        f"| `{col_name}` | `{col_type}` | {not_null} | `{default_rendered}` | {pk} | {nulls} | {distinct} | {min_t} | {max_t} |"
                    )

                cur.execute(f"PRAGMA index_list({quoted})")
                indexes = cur.fetchall()
                lines.extend(["", "Indexes:", ""])
                if not indexes:
                    lines.append("- None")
                for index in indexes:
                    lines.append(f"- `{index[1]}` unique={index[2]} origin={index[3]}")

            conn.close()
        except Exception as exc:
            lines.append(f"- ERROR opening database: `{exc}`")

    (LOG_DIR / "02_db_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_log_message(line: str) -> str:
    line = redact_text(line.strip())
    line = LOG_TIME_RE.sub("<time>", line)
    line = re.sub(r"\b\d+(?:\.\d+)?\b", "<num>", line)
    if len(line) > 220:
        line = line[:217] + "..."
    return line


def write_log_audit(files: list[FileInfo]) -> None:
    log_infos = [
        info
        for info in files
        if info.category == "log"
        or info.rel.startswith("data/bot/logs/")
        or info.rel.endswith("/raw/full_debug.log")
    ]
    aggregate = Counter()
    severity_by_file: dict[str, Counter[str]] = {}
    examples: dict[str, list[str]] = defaultdict(list)
    file_ranges: dict[str, tuple[str, str]] = {}
    top_messages = Counter()

    for info in log_infos:
        counts = Counter()
        first_ts = ""
        last_ts = ""
        try:
            with info.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    counts["lines"] += 1
                    match = LOG_TIME_RE.search(line)
                    if match:
                        if not first_ts:
                            first_ts = match.group("ts")
                        last_ts = match.group("ts")
                    checks = [
                        ("errors", ERROR_RE),
                        ("warnings", WARNING_RE),
                        ("disconnects", DISCONNECT_RE),
                        ("rate_limits", RATE_LIMIT_RE),
                        ("rejects", REJECT_RE),
                        ("signals", SIGNAL_RE),
                    ]
                    for label, regex in checks:
                        if regex.search(line):
                            counts[label] += 1
                            aggregate[label] += 1
                            if len(examples[label]) < 20:
                                examples[label].append(f"{info.rel}: {redact_text(line.strip())[:300]}")
                    if ERROR_RE.search(line) or WARNING_RE.search(line):
                        top_messages[normalize_log_message(line)] += 1
        except OSError as exc:
            counts["read_errors"] += 1
            examples["read_errors"].append(f"{info.rel}: {exc}")
        severity_by_file[info.rel] = counts
        file_ranges[info.rel] = (first_ts, last_ts)

    lines = [
        "# Phase 0.4 Log Audit",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: every discovered .log file and raw full_debug.log was streamed and pattern-scanned.",
        "- Confirmed: secret-like tokens are redacted from examples.",
        "- Inference: severity classification is regex-based and can over-count benign strings containing words like failed or timeout.",
        "",
        "## Summary",
        "",
        f"- Log files scanned: {len(log_infos)}",
        f"- Total log bytes: {sum(info.size for info in log_infos):,}",
        f"- Error-like lines: {aggregate['errors']}",
        f"- Warning-like lines: {aggregate['warnings']}",
        f"- Disconnect/WS/timeout-like lines: {aggregate['disconnects']}",
        f"- Rate-limit-like lines: {aggregate['rate_limits']}",
        f"- Reject-like lines: {aggregate['rejects']}",
        f"- Signal/delivery-like lines: {aggregate['signals']}",
        "",
        "## Files",
        "",
        "| Path | Bytes | Lines | First Timestamp | Last Timestamp | Errors | Warnings | Disconnects | Rate Limits | Rejects | Signals |",
        "| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for info in log_infos:
        counts = severity_by_file.get(info.rel, Counter())
        first_ts, last_ts = file_ranges.get(info.rel, ("", ""))
        lines.append(
            f"| {markdown_link(info.path)} | {info.size:,} | {counts['lines']} | {first_ts} | {last_ts} | {counts['errors']} | {counts['warnings']} | {counts['disconnects']} | {counts['rate_limits']} | {counts['rejects']} | {counts['signals']} |"
        )

    lines.extend(["", "## Top Error/Warning Message Shapes", "", "| Count | Normalized Message |", "| ---: | --- |"])
    for message, count in top_messages.most_common(50):
        lines.append(f"| {count} | `{message}` |")

    for label in ["errors", "warnings", "disconnects", "rate_limits", "rejects", "signals", "read_errors"]:
        lines.extend(["", f"## Sample {label.replace('_', ' ').title()}", ""])
        if examples.get(label):
            for sample in examples[label]:
                lines.append(f"- `{sample}`")
        else:
            lines.append("None captured.")

    (LOG_DIR / "03_log_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_event_time(row: dict[str, Any]) -> str:
    for key in ("timestamp", "time", "created_at", "updated_at", "event_time", "detected_at", "closed_at"):
        if key in row and row[key] is not None:
            return normalize_time_value(row[key])
    return ""


def hour_bucket(value: str) -> str:
    if not value:
        return "unknown"
    match = re.match(r"^(20\d\d-\d\d-\d\d[T ]\d\d)", value)
    if match:
        return match.group(1).replace(" ", "T")
    match = re.match(r"^(20\d\d-\d\d-\d\d)", value)
    if match:
        return match.group(1)
    return "unknown"


def row_setup(row: dict[str, Any]) -> str:
    return str(row.get("setup_id") or row.get("strategy_id") or row.get("strategy") or row.get("setup") or "unknown")


def row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "unknown")


def write_telemetry_audit(files: list[FileInfo]) -> None:
    telemetry_infos = [
        info
        for info in files
        if info.category == "telemetry"
        and info.suffix in {".jsonl", ".json"}
    ]

    file_stats: dict[str, Counter[str]] = {}
    json_errors: dict[str, list[str]] = defaultdict(list)
    by_artifact = Counter()
    by_run = Counter()
    time_buckets = Counter()
    decision_status = Counter()
    decision_signals = Counter()
    decision_rejects = Counter()
    decision_total = Counter()
    decision_reasons = Counter()
    symbol_decision_status = Counter()
    symbol_decision_signals = Counter()
    symbol_decision_rejects = Counter()
    symbol_decision_total = Counter()
    rejected_rows_by_setup = Counter()
    rejection_reasons = Counter()
    candidate_by_setup = Counter()
    selected_by_setup = Counter()
    tracking_events = Counter()
    tracking_outcomes = Counter()
    data_quality_missing = Counter()
    data_quality_invalid = Counter()
    cycle_metrics = Counter()
    shortlist_sources = Counter()
    per_run_records = defaultdict(Counter)
    samples = defaultdict(list)

    for info in telemetry_infos:
        stats = Counter()
        artifact = info.path.name
        by_artifact[artifact] += 1
        run_name = "current"
        parts = info.rel.split("/")
        if "runs" in parts:
            idx = parts.index("runs")
            if idx + 1 < len(parts):
                run_name = parts[idx + 1]
        by_run[run_name] += 1

        def handle_row(row: dict[str, Any]) -> None:
            stats["records"] += 1
            per_run_records[run_name][artifact] += 1
            ts = get_event_time(row)
            time_buckets[hour_bucket(ts)] += 1

            setup = row_setup(row)
            symbol = row_symbol(row)
            status = str(row.get("status") or row.get("event") or row.get("type") or row.get("outcome") or "unknown")
            reason = str(row.get("reason_code") or row.get("reason") or row.get("reject_reason") or "unknown")

            if artifact.startswith("strategy_decisions"):
                decision_total[setup] += 1
                decision_status[(setup, status)] += 1
                symbol_decision_total[symbol] += 1
                symbol_decision_status[(symbol, status)] += 1
                if status in {"signal", "selected", "candidate"}:
                    decision_signals[setup] += 1
                    symbol_decision_signals[symbol] += 1
                else:
                    decision_rejects[setup] += 1
                    symbol_decision_rejects[symbol] += 1
                    decision_reasons[(setup, reason)] += 1
                    rejection_reasons[reason] += 1
                    if len(samples[f"strategy_reason:{reason}"]) < 3:
                        samples[f"strategy_reason:{reason}"].append(row)

            elif artifact.startswith("rejected"):
                rejected_rows_by_setup[setup] += 1
                rejection_reasons[reason] += 1
                if len(samples[f"rejected:{reason}"]) < 3:
                    samples[f"rejected:{reason}"].append(row)

            elif artifact == "candidates.jsonl":
                candidate_by_setup[setup] += 1

            elif artifact == "selected.jsonl":
                selected_by_setup[setup] += 1

            elif artifact == "tracking_events.jsonl":
                event = str(
                    row.get("event_type")
                    or row.get("event")
                    or row.get("status")
                    or row.get("type")
                    or "unknown"
                )
                outcome = str(
                    row.get("outcome")
                    or row.get("exit_reason")
                    or row.get("event_type")
                    or row.get("status")
                    or "unknown"
                )
                tracking_events[event] += 1
                tracking_outcomes[outcome] += 1

            elif artifact == "data_quality.jsonl":
                missing = row.get("missing_fields") if isinstance(row.get("missing_fields"), list) else []
                invalid = row.get("invalid_fields") if isinstance(row.get("invalid_fields"), list) else []
                data_quality_missing.update(str(item) for item in missing)
                data_quality_invalid.update(str(item) for item in invalid)

            elif artifact == "cycles.jsonl":
                for key in ("cycles", "detector_runs", "candidates", "delivered", "symbols_processed"):
                    value = row.get(key)
                    if isinstance(value, (int, float)):
                        cycle_metrics[key] += int(value)

            elif artifact == "shortlist.jsonl":
                source = str(row.get("source") or row.get("mode") or row.get("reason") or "unknown")
                shortlist_sources[source] += 1

        try:
            if info.suffix == ".jsonl":
                with info.path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, 1):
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            stats["json_errors"] += 1
                            if len(json_errors[info.rel]) < 5:
                                json_errors[info.rel].append(f"line {line_no}: {exc}")
                            continue
                        if isinstance(row, dict):
                            handle_row(row)
                        else:
                            stats["non_object_records"] += 1
            else:
                try:
                    data = json.loads(info.path.read_text(encoding="utf-8"))
                    stats["records"] += 1
                    if isinstance(data, dict):
                        handle_row(data)
                    else:
                        stats["non_object_records"] += 1
                except json.JSONDecodeError as exc:
                    stats["json_errors"] += 1
                    json_errors[info.rel].append(str(exc))
        except OSError as exc:
            stats["read_errors"] += 1
            json_errors[info.rel].append(str(exc))
        file_stats[info.rel] = stats

    zero_hit = sorted(set(decision_total) - set(decision_signals))

    lines = [
        "# Phase 0.5 Telemetry Audit",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: every discovered telemetry .jsonl/.json file under data/bot/telemetry was streamed or parsed.",
        "- Confirmed: aggregation covers all rows that parsed as JSON objects.",
        "- Uncertainty: win rate and SL rate are only reportable if tracking events persist explicit outcome/exit fields.",
        "- Inference: zero-hit setups are inferred from strategy_decisions rows with no signal-like status across scanned telemetry.",
        "",
        "## Summary",
        "",
        f"- Telemetry files scanned: {len(telemetry_infos)}",
        f"- Parsed telemetry records: {sum(stats['records'] for stats in file_stats.values()):,}",
        f"- JSON parse errors: {sum(stats['json_errors'] for stats in file_stats.values()):,}",
        f"- Runs/directories represented: {len(by_run)}",
        "",
        "### Artifact Counts",
        "",
        "| Artifact | Files | Records |",
        "| --- | ---: | ---: |",
    ]
    artifact_records = Counter()
    for info in telemetry_infos:
        artifact_records[info.path.name] += file_stats.get(info.rel, Counter())["records"]
    for artifact, count in by_artifact.most_common():
        lines.append(f"| {artifact} | {count} | {artifact_records[artifact]:,} |")

    lines.extend(["", "## Runs", "", "| Run | Files | Records |", "| --- | ---: | ---: |"])
    for run_name, file_count in by_run.most_common():
        records = sum(per_run_records[run_name].values())
        lines.append(f"| {run_name} | {file_count} | {records:,} |")

    lines.extend(["", "## Strategy Metrics", "", "| Strategy | Decision Rows | Signal Decisions | Non-Signal Decisions | Signal Rate | Rejected File Rows | Candidate Rows | Selected Rows | Top Decision Reject Reason |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
    for setup, total in decision_total.most_common():
        signals = decision_signals[setup]
        rejects = decision_rejects[setup]
        rate = (signals / total * 100) if total else 0.0
        reasons = Counter({reason: count for (s, reason), count in decision_reasons.items() if s == setup})
        top_reason = reasons.most_common(1)[0][0] if reasons else ""
        lines.append(f"| {setup} | {total:,} | {signals:,} | {rejects:,} | {rate:.2f}% | {rejected_rows_by_setup[setup]:,} | {candidate_by_setup[setup]:,} | {selected_by_setup[setup]:,} | {top_reason} |")

    lines.extend(["", "### Zero-Hit Strategies", ""])
    if zero_hit:
        for setup in zero_hit:
            lines.append(f"- {setup}")
    else:
        lines.append("No zero-hit strategies inferred from scanned strategy_decisions.")

    lines.extend(["", "## Rejection Reasons", "", "| Reason | Count |", "| --- | ---: |"])
    for reason, count in rejection_reasons.most_common(60):
        lines.append(f"| {reason} | {count:,} |")

    lines.extend(["", "## Symbol Metrics", "", "| Symbol | Decision Rows | Signal Decisions | Non-Signal Decisions | Signal Rate |", "| --- | ---: | ---: | ---: | ---: |"])
    for symbol, total in symbol_decision_total.most_common(80):
        signals = symbol_decision_signals[symbol]
        rejects = symbol_decision_rejects[symbol]
        rate = (signals / total * 100) if total else 0.0
        lines.append(f"| {symbol} | {total:,} | {signals:,} | {rejects:,} | {rate:.2f}% |")

    lines.extend(["", "## Tracking Outcomes", "", "| Outcome/Event | Count |", "| --- | ---: |"])
    if tracking_events or tracking_outcomes:
        for outcome, count in (tracking_outcomes + tracking_events).most_common(50):
            lines.append(f"| {outcome} | {count:,} |")
    else:
        lines.append("| no_tracking_events_observed | 0 |")

    sl_count = sum(count for outcome, count in tracking_outcomes.items() if "sl" in outcome.lower() or "stop" in outcome.lower())
    win_count = sum(count for outcome, count in tracking_outcomes.items() if "tp" in outcome.lower() or "win" in outcome.lower() or "profit" in outcome.lower())
    closed_count = sl_count + win_count
    lines.extend(["", "### Outcome Rates", ""])
    if closed_count:
        lines.append(f"- Inferred SL-like rate: {sl_count / closed_count * 100:.2f}% ({sl_count}/{closed_count})")
        lines.append(f"- Inferred TP/win-like rate: {win_count / closed_count * 100:.2f}% ({win_count}/{closed_count})")
    else:
        lines.append("- Not observable from scanned tracking events: no explicit SL/TP-like outcomes found.")

    lines.extend(["", "## Data Quality Fields", "", "### Missing", "", "| Field | Count |", "| --- | ---: |"])
    for field, count in data_quality_missing.most_common(50):
        lines.append(f"| {field} | {count:,} |")
    if not data_quality_missing:
        lines.append("| none_observed | 0 |")

    lines.extend(["", "### Invalid", "", "| Field | Count |", "| --- | ---: |"])
    for field, count in data_quality_invalid.most_common(50):
        lines.append(f"| {field} | {count:,} |")
    if not data_quality_invalid:
        lines.append("| none_observed | 0 |")

    lines.extend(["", "## Temporal Distribution", "", "| Hour/Date | Records |", "| --- | ---: |"])
    for bucket, count in sorted(time_buckets.items()):
        lines.append(f"| {bucket} | {count:,} |")

    lines.extend(["", "## File Detail", "", "| Path | Records | JSON Errors | Read Errors |", "| --- | ---: | ---: | ---: |"])
    for info in telemetry_infos:
        stats = file_stats.get(info.rel, Counter())
        lines.append(f"| {markdown_link(info.path)} | {stats['records']:,} | {stats['json_errors']} | {stats['read_errors']} |")

    if json_errors:
        lines.extend(["", "## JSON Parse Error Samples", ""])
        for path, errors in sorted(json_errors.items()):
            lines.append(f"### {path}")
            for error in errors:
                lines.append(f"- `{error}`")

    lines.extend(["", "## Bottleneck Inferences", ""])
    if rejection_reasons:
        top_reason, top_count = rejection_reasons.most_common(1)[0]
        total_rejects = sum(rejection_reasons.values())
        lines.append(f"- Top rejection reason: `{top_reason}` ({top_count:,}/{total_rejects:,}, {top_count / total_rejects * 100:.2f}%).")
    if zero_hit:
        lines.append(f"- Zero-hit strategy count: {len(zero_hit)}. Treat as calibration candidates only after checking whether each setup was enabled and fed valid fields.")
    if data_quality_missing or data_quality_invalid:
        lines.append("- Data-quality rows exist; inspect missing/invalid fields before lowering strategy thresholds.")
    if not (data_quality_missing or data_quality_invalid):
        lines.append("- No missing/invalid data-quality fields were observed in parsed data_quality telemetry.")

    (LOG_DIR / "04_telemetry_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Phase 0 forensic audit reports.")
    parser.add_argument(
        "--include-git",
        action="store_true",
        help="include .git files in the manifest and hashes; normally excluded as VCS storage",
    )
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = collect_file_info(include_git=args.include_git)
    write_file_map(files)
    write_config_audit(files)
    write_db_audit(files)
    write_log_audit(files)
    write_telemetry_audit(files)

    for name in [
        "00_file_map.md",
        "01_config_audit.md",
        "02_db_audit.md",
        "03_log_audit.md",
        "04_telemetry_audit.md",
    ]:
        print(LOG_DIR / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
