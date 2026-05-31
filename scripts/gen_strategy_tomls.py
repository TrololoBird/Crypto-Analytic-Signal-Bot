"""One-shot: emit config/strategies/<setup_id>.toml from catalog defaults."""
from __future__ import annotations

from pathlib import Path

from bot.domain.strategy_catalog import CATALOG_ENTRIES

ALIASES = {"fvg": "fvg_setup"}


def main() -> None:
    out = Path("config/strategies")
    out.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in out.glob("*.toml")}
    for entry in CATALOG_ENTRIES:
        setup_id = entry.setup_id
        if setup_id in existing:
            continue
        if any(ALIASES.get(stem) == setup_id for stem in existing):
            continue
        text = f"""# {setup_id} — catalog defaults (ops-tunable)

[strategy]
enabled = true
name = "{setup_id}"

[scoring]
base_score = {entry.base_score}

[filters]
min_volume_ratio = {entry.min_volume_ratio}
min_adx = {entry.min_adx_1h}

[risk_management]
sl_buffer_atr = 0.85
min_rr = {entry.min_rr}
"""
        path = out / f"{setup_id}.toml"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
