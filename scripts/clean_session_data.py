#!/usr/bin/env python3
"""Remove persisted runtime artifacts so a new bot session does not mix with old runs."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import bootstrap_repo_path, configure_script_logging

from bot.domain.config import load_settings

LOG = configure_script_logging("scripts.clean_session_data")

_TELEMETRY_SUBDIRS = (
    "runs",
    "replay",
    "analysis",
    "raw",
    "features",
    "market_history",
)


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError as exc:
        LOG.warning("remove_failed", path=str(path), error=str(exc))
        return False


def _clear_directory_contents(directory: Path) -> int:
    if not directory.exists():
        return 0
    removed = 0
    for child in directory.iterdir():
        if _remove_path(child):
            removed += 1
    return removed


def clean_telemetry(telemetry_dir: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    for name in _TELEMETRY_SUBDIRS:
        target = telemetry_dir / name
        stats[f"telemetry/{name}"] = _clear_directory_contents(target)
    return stats


def clean_live_watch(live_watch_dir: Path) -> int:
    return _clear_directory_contents(live_watch_dir)


def clean_logs(logs_dir: Path, *, keep_latest: int = 0) -> int:
    if not logs_dir.exists():
        return 0
    files = sorted(
        (path for path in logs_dir.glob("bot_*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for idx, path in enumerate(files):
        if idx < keep_latest:
            continue
        if _remove_path(path):
            removed += 1
    return removed


def reset_sqlite(db_path: Path) -> int:
    removed = 0
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if _remove_path(path):
            removed += 1
    return removed


def clean_session_artifacts(
    settings: object,
    *,
    mode: str,
    keep_logs: int = 0,
    dry_run: bool = False,
) -> dict[str, int]:
    data_dir = Path(settings.data_dir)
    telemetry_dir = Path(settings.telemetry_dir)
    logs_dir = Path(settings.logs_dir)
    db_path = Path(settings.db_path)
    live_watch_dir = data_dir.parent / "live_watch"

    stats: dict[str, int] = {}

    def _apply(label: str, count: int) -> None:
        stats[label] = count
        if count:
            LOG.info("clean_session_removed", target=label, count=count)

    if dry_run:
        for name in _TELEMETRY_SUBDIRS:
            target = telemetry_dir / name
            if target.exists():
                stats[f"telemetry/{name}"] = sum(1 for _ in target.iterdir())
        if live_watch_dir.exists():
            stats["live_watch"] = sum(1 for _ in live_watch_dir.iterdir())
        stats["logs"] = sum(1 for _ in logs_dir.glob("bot_*.log"))
        if mode == "full":
            stats["sqlite"] = sum(
                1
                for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
                if path.exists()
            )
        return stats

    telemetry_stats = clean_telemetry(telemetry_dir)
    for key, value in telemetry_stats.items():
        _apply(key, value)

    if mode in {"smoke", "full"}:
        _apply("live_watch", clean_live_watch(live_watch_dir))
        _apply("logs", clean_logs(logs_dir, keep_latest=keep_logs))
        for rel in ("session", "public_audit"):
            _apply(rel, _clear_directory_contents(data_dir / rel))
        for filename in ("features_store.json", "quality_monitor.json"):
            if _remove_path(data_dir / filename):
                _apply(filename, 1)

    if mode == "full":
        _apply("sqlite", reset_sqlite(db_path))
        repo_root = Path(__file__).resolve().parents[1]
        migrations = repo_root / "scripts" / "apply_migrations.py"
        if migrations.exists() and not dry_run:
            import subprocess

            subprocess.run(
                [sys.executable, str(migrations)],
                cwd=str(repo_root),
                check=False,
            )
            config_path = Path(getattr(settings, "config_path", Path("config.toml")))
            init_repo = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import asyncio; from pathlib import Path; "
                        "from bot.domain.config import load_settings; "
                        "from bot.persistence.repository.memory import MemoryRepository; "
                        f"async def _init():\n"
                        f"    s = load_settings(config_path=Path({config_path!r}));\n"
                        "    repo = MemoryRepository(db_path=s.db_path, data_dir=s.data_dir / 'parquet');\n"
                        "    await repo.initialize();\n"
                        "    await repo.close();\n"
                        "asyncio.run(_init())"
                    ),
                ],
                cwd=str(repo_root),
                check=False,
            )
            if init_repo.returncode != 0:
                LOG.warning("repository schema init after full clean failed")

    return stats


def main() -> int:
    bootstrap_repo_path()
    parser = argparse.ArgumentParser(
        description="Clean persisted bot session data (telemetry runs, live_watch, optional DB)."
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )
    parser.add_argument(
        "--mode",
        choices=("telemetry", "smoke", "full"),
        default="smoke",
        help=(
            "telemetry=JSONL runs only; smoke=telemetry+live_watch+logs+session caches; "
            "full=smoke+reset bot.db"
        ),
    )
    parser.add_argument(
        "--keep-logs",
        type=int,
        default=0,
        help="Keep N newest bot_*.log files when mode is smoke/full",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts without deleting")
    args = parser.parse_args()

    settings = load_settings(config_path=Path(args.config))
    stats = clean_session_artifacts(
        settings,
        mode=args.mode,
        keep_logs=max(0, int(args.keep_logs)),
        dry_run=bool(args.dry_run),
    )
    total = sum(stats.values())
    action = "would_remove" if args.dry_run else "removed"
    print(f"[OK] session cleanup ({action}) | mode={args.mode} items={total} detail={stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
