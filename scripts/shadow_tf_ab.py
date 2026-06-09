#!/usr/bin/env python3
"""Shadow TF A/B telemetry writer — records variant tags without extra Telegram delivery."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

PILOT_SETUPS = ("fvg_setup", "structure_pullback", "ema_bounce")
VARIANTS = ("catalog_entry_tf", "alt_15m", "alt_1h")


def record_shadow_event(
    *,
    telemetry_dir: Path,
    run_id: str,
    setup_id: str,
    symbol: str,
    variant: str,
    entry_tf: str,
    expired: bool | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    sl_noise: bool | None = None,
) -> Path:
    """Append one shadow A/B row to telemetry JSONL (Phase 5 methodology)."""
    out_dir = telemetry_dir / "shadow_tf_ab" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "events.jsonl"
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "setup_id": setup_id,
        "symbol": symbol,
        "ab_variant": variant,
        "entry_tf_used": entry_tf,
        "expired": expired,
        "mfe": mfe,
        "mae": mae,
        "sl_noise": sl_noise,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Shadow TF A/B telemetry helper")
    parser.add_argument("--telemetry-dir", type=Path, default=Path("data/bot/telemetry"))
    parser.add_argument("--run-id", type=str, default=datetime.now(UTC).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--init-manifest", action="store_true")
    args = parser.parse_args()
    if not args.init_manifest:
        parser.print_help()
        return
    manifest = {
        "run_id": args.run_id,
        "pilot_setups": list(PILOT_SETUPS),
        "variants": list(VARIANTS),
        "created_at": datetime.now(UTC).isoformat(),
        "notes": "Shadow detect only; no Telegram spam. Compare expired%/SL-noise after supervised session.",
    }
    out = args.telemetry_dir / "shadow_tf_ab" / args.run_id / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"manifest written: {out}")


if __name__ == "__main__":
    main()
