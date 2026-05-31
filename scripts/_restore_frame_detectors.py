from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAK = ROOT / "bot/setups/detectors/patterns.py.bak"
bak = BAK.read_text(encoding="utf-8")

PAIRS = [
    ("absorption.py", "detect_absorption", "detect_absorption_prepared"),
    ("aggression_shift.py", "detect_aggression_shift", "detect_aggression_shift_prepared"),
    ("wyckoff_spring.py", "detect_wyckoff_spring", "detect_wyckoff_spring_prepared"),
    ("stop_hunt.py", "detect_stop_hunt", "detect_stop_hunt_prepared"),
]


def extract(name: str) -> str:
    m = re.search(rf"def {name}\(.*?(?=\ndef )", bak, re.S)
    if not m:
        raise SystemExit(f"missing {name}")
    return m.group(0)


for fname, frame_fn, prep_fn in PAIRS:
    path = ROOT / "bot/setups/detectors" / fname
    prep = path.read_text(encoding="utf-8")
    idx = prep.find(f"def {prep_fn}")
    prepared = prep[idx:] if idx >= 0 else ""
    frame = extract(frame_fn)
    header = (
        '"""Spec + prepared detector."""\n'
        "from __future__ import annotations\n\n"
        "import polars as pl\n\n"
        "from ._common import SpecHit, as_float, finite_or_none, with_spec_columns, _latest_values, build_spec_signal\n"
        "from ...domain.config import BotSettings\n"
        "from ...domain.schemas import PreparedSymbol, Signal\n"
        "from ...domain.strategy_catalog import catalog_default_params\n"
        "from ._roadmap import _build_atr_signal, _flow_delta_with_source, _last, _reject, _as_float\n\n"
        f"__all__ = ['{frame_fn}', '{prep_fn}']\n\n\n"
    )
    if fname == "wyckoff_spring.py":
        header = header.replace(
            "from ._roadmap import _build_atr_signal, _flow_delta_with_source, _last, _reject, _as_float\n",
            "from ._roadmap import _build_atr_signal, _last, _reject, _as_float\n",
        )
    if fname == "stop_hunt.py":
        header = header.replace(
            "from ._roadmap import _build_atr_signal, _flow_delta_with_source, _last, _reject, _as_float\n",
            "from ._roadmap import _build_atr_signal, _last, _reject, _as_float\n",
        )
    prepared = prepared.replace("defaults=self.DEFAULTS", "defaults=catalog_default_params(setup_id)")
    path.write_text(header + frame + "\n\n\n" + prepared, encoding="utf-8")
    print("ok", fname)
