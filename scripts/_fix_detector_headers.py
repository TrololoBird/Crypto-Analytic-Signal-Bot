from __future__ import annotations

import re
from pathlib import Path

d = Path(__file__).resolve().parents[1] / "bot" / "setups" / "detectors"
for path in sorted(d.glob("*.py")):
    if path.name.startswith("_") or path.name == "patterns.py":
        continue
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^def ", text, re.M)
    if not match:
        continue
    body = text[match.start() :]
    needs = ["SpecHit", "as_float", "with_spec_columns"]
    for sym in (
        "_pivot_rows",
        "_latest_values",
        "_row_volume_ratio",
        "_clean_impulse",
        "_valid_order_block_rows",
        "finite_or_none",
        "required_columns",
    ):
        if sym in body:
            needs.append(sym)
    pub = re.search(r"^def (detect_\w+)", body, re.M)
    pub_name = pub.group(1) if pub else "detect_unknown"
    imp = ", ".join(dict.fromkeys(needs))
    new = (
        '"""Spec detector — see STRATEGY_CATALOG."""\n'
        "from __future__ import annotations\n\n"
        "import polars as pl\n\n"
        f"from ._common import {imp}\n\n"
        f'__all__ = ["{pub_name}"]\n\n'
        + body
    )
    path.write_text(new, encoding="utf-8")
    print("fixed", path.name)
