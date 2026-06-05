#!/usr/bin/env python3
"""Compare strategy code defaults vs config/strategies/*.toml drift.

Emits JSON drift report and a TOML patch file. Exits 1 when drift is found
unless --report-only is set.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

import tomllib

from bot.domain.strategy_catalog import catalog_default_params
from bot.runtime.errors import DEFENSIVE_EXC
from bot.strategies import STRATEGY_CLASSES

LOG = logging.getLogger("scripts.reconcile_strategy_defaults")

RECONCILE_KEYS = ("base_score", "min_rr", "sl_buffer_atr")
TOML_SECTION_BY_KEY = {
    "base_score": ("scoring", "base_score"),
    "min_rr": ("risk_management", "min_rr"),
    "sl_buffer_atr": ("risk_management", "sl_buffer_atr"),
}


@dataclass(frozen=True, slots=True)
class DefaultDrift:
    setup_id: str
    field: str
    code_value: float | None
    toml_value: float | None
    toml_path: str | None
    delta: float | None
    status: str


def _strategy_toml_map(config_dir: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for path in sorted(config_dir.glob("*.toml")):
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            LOG.warning("skip unreadable toml | path=%s err=%s", path, exc)
            continue
        strategy = payload.get("strategy")
        if not isinstance(strategy, dict):
            continue
        name = strategy.get("name")
        if isinstance(name, str) and name.strip():
            mapping[name.strip()] = path
    return mapping


def _toml_value(path: Path, field: str) -> float | None:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    section_name, key = TOML_SECTION_BY_KEY[field]
    section = payload.get(section_name)
    if not isinstance(section, dict):
        return None
    value = section.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _code_defaults(setup_cls: type) -> dict[str, float]:
    setup_id = str(getattr(setup_cls, "setup_id", "") or "")
    defaults = getattr(setup_cls, "DEFAULTS", {}) or {}
    params: dict[str, float] = {}
    try:
        instance = setup_cls()
        optimizable = instance.get_optimizable_params(None)
        if isinstance(optimizable, dict):
            for key in RECONCILE_KEYS:
                value = optimizable.get(key)
                if isinstance(value, (int, float)):
                    params[key] = float(value)
    except DEFENSIVE_EXC:
        LOG.debug("get_optimizable_params failed | setup=%s", setup_id, exc_info=True)
    catalog = catalog_default_params(setup_id)
    for key in RECONCILE_KEYS:
        if key in params:
            continue
        value = defaults.get(key)
        if isinstance(value, (int, float)):
            params[key] = float(value)
            continue
        cat_value = catalog.get(key)
        if isinstance(cat_value, (int, float)):
            params[key] = float(cat_value)
    return params


def collect_defaults_drift(
    *,
    config_dir: Path | None = None,
    tolerance: float = 1e-6,
) -> list[DefaultDrift]:
    root = config_dir or Path("config/strategies")
    toml_by_setup = _strategy_toml_map(root)
    rows: list[DefaultDrift] = []
    for setup_cls in STRATEGY_CLASSES:
        setup_id = str(getattr(setup_cls, "setup_id", "") or "")
        code_defaults = _code_defaults(setup_cls)
        toml_path = toml_by_setup.get(setup_id)
        for field in RECONCILE_KEYS:
            code_value = code_defaults.get(field)
            if code_value is None:
                rows.append(
                    DefaultDrift(
                        setup_id=setup_id,
                        field=field,
                        code_value=None,
                        toml_value=None,
                        toml_path=str(toml_path) if toml_path else None,
                        delta=None,
                        status="missing_code_value",
                    )
                )
                continue
            if toml_path is None:
                rows.append(
                    DefaultDrift(
                        setup_id=setup_id,
                        field=field,
                        code_value=code_value,
                        toml_value=None,
                        toml_path=None,
                        delta=None,
                        status="missing_toml",
                    )
                )
                continue
            toml_value = _toml_value(toml_path, field)
            if toml_value is None:
                rows.append(
                    DefaultDrift(
                        setup_id=setup_id,
                        field=field,
                        code_value=code_value,
                        toml_value=None,
                        toml_path=str(toml_path),
                        delta=None,
                        status="missing_toml_value",
                    )
                )
                continue
            delta = code_value - toml_value
            status = "ok" if abs(delta) <= tolerance else "drift"
            rows.append(
                DefaultDrift(
                    setup_id=setup_id,
                    field=field,
                    code_value=code_value,
                    toml_value=toml_value,
                    toml_path=str(toml_path),
                    delta=round(delta, 6),
                    status=status,
                )
            )
    return rows


def write_drift_report(
    rows: list[DefaultDrift],
    *,
    output: Path,
) -> dict[str, Any]:
    drift_rows = [row for row in rows if row.status == "drift"]
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total": len(rows),
            "drift": len(drift_rows),
            "missing_toml": sum(1 for row in rows if row.status == "missing_toml"),
            "missing_toml_value": sum(1 for row in rows if row.status == "missing_toml_value"),
            "missing_code_value": sum(1 for row in rows if row.status == "missing_code_value"),
        },
        "drift": [asdict(row) for row in drift_rows],
        "rows": [asdict(row) for row in rows],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def write_toml_patch(
    rows: list[DefaultDrift],
    *,
    output: Path,
) -> Path:
    drift_rows = [row for row in rows if row.status == "drift"]
    lines = [
        "# Generated by scripts/reconcile_strategy_defaults.py",
        f"# generated_at = {datetime.now(UTC).isoformat()}",
        "",
    ]
    if not drift_rows:
        lines.append("# No drift — patch file is empty.")
    for row in drift_rows:
        section_name, key = TOML_SECTION_BY_KEY[row.field]
        rel_path = row.toml_path or ""
        lines.extend(
            [
                "[[patch]]",
                f'setup_id = "{row.setup_id}"',
                f'toml_path = "{rel_path}"',
                f'field = "{row.field}"',
                f"code_value = {row.code_value}",
                f"toml_value = {row.toml_value}",
                "",
                f"[{section_name}]",
                f"{key} = {row.code_value}  # was {row.toml_value}",
                "",
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config/strategies"),
        help="Directory with per-strategy TOML defaults",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/bot/reports/strategy_defaults_drift.json"),
    )
    parser.add_argument(
        "--patch-output",
        type=Path,
        default=Path("data/bot/reports/config_strategies.toml.patch"),
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-6,
        help="Absolute delta treated as drift",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0 after writing reports (even when drift exists)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    rows = collect_defaults_drift(config_dir=args.config_dir, tolerance=args.tolerance)
    payload = write_drift_report(rows, output=args.output)
    patch_path = write_toml_patch(rows, output=args.patch_output)
    drift_count = int(payload["summary"]["drift"])
    LOG.info(
        "defaults drift report | path=%s patch=%s drift=%d total=%d",
        args.output,
        patch_path,
        drift_count,
        payload["summary"]["total"],
    )
    for row in payload["drift"]:
        LOG.warning(
            "defaults drift | setup=%s field=%s code=%s toml=%s delta=%s path=%s",
            row["setup_id"],
            row["field"],
            row["code_value"],
            row["toml_value"],
            row["delta"],
            row["toml_path"],
        )
    if drift_count and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
