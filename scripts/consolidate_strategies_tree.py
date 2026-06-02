"""Merge bot/setups/detectors/* into bot/strategies/* and remove duplicate tree."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRAT = ROOT / "bot" / "strategies"
DET = ROOT / "bot" / "setups" / "detectors"

# strategy module -> detector module(s) to inline (order matters)
MERGE_MAP: dict[str, list[str]] = {
    "order_block.py": ["ob.py"],
    "fvg.py": ["fvg_setup.py"],
    "volume_climax_reversal.py": ["volume_climax.py"],
    "wick_trap_reversal.py": ["wick_trap.py", "wick_trap_reversal.py"],
    "stop_hunt_detection.py": ["stop_hunt.py", "stop_hunt_detection.py"],
    "vwap_trend.py": ["vwap.py", "vwap_trend.py"],
}

DETECTOR_IMPORT_RE = re.compile(r"^from \.\.setups\.detectors(?:\.[\w]+)? import .+$", re.M)


def fix_detector_imports(text: str) -> str:
    out = text
    for old, new in (
        ("from ...domain.", "from ..domain."),
        ("from ...features.", "from ..features."),
        ("from ...strategies.", "from ."),
        ("from .. import ", "from ..setups import "),
        ("from ..spec_runtime", "from ..setups.spec_runtime"),
        ("from ..utils", "from ..setups.utils"),
    ):
        out = out.replace(old, new)
    return out


def detector_modules_for(strategy_file: str) -> list[str]:
    if strategy_file in MERGE_MAP:
        return MERGE_MAP[strategy_file]
    base = strategy_file
    det = DET / base
    if det.is_file():
        return [base]
    return []


def merge_detector_bodies(modules: list[str]) -> str:
    parts: list[str] = []
    for mod in modules:
        path = DET / mod
        if not path.is_file():
            raise FileNotFoundError(path)
        body = fix_detector_imports(path.read_text(encoding="utf-8"))
        parts.append(body.strip())
    return "\n\n\n".join(parts)


def split_wrapper(strategy_src: str) -> tuple[str, str]:
    """Return (header_imports, class_and_tail) without detector imports."""
    lines = strategy_src.splitlines()
    header: list[str] = []
    body: list[str] = []
    in_class = False
    for line in lines:
        if line.startswith("from ..setups.detectors"):
            continue
        if DETECTOR_IMPORT_RE.match(line):
            continue
        if line.startswith("class ") and "Setup" in line:
            in_class = True
        if in_class:
            body.append(line)
        else:
            header.append(line)
    return "\n".join(header).strip(), "\n".join(body).strip()


def consolidate_strategy(strategy_file: str) -> bool:
    strat_path = STRAT / strategy_file
    if not strat_path.is_file():
        return False
    modules = detector_modules_for(strategy_file)
    if not modules:
        return False
    wrapper = strat_path.read_text(encoding="utf-8")
    if "setups.detectors" not in wrapper:
        return False
    detector_body = merge_detector_bodies(modules)
    header, setup_class = split_wrapper(wrapper)
    detector_body = re.sub(
        r"^from __future__ import annotations\s*\n",
        "",
        detector_body,
        flags=re.M,
    )
    detector_body = re.sub(r'^"""[\s\S]*?"""\s*\n', "", detector_body, count=len(modules))
    merged = f"{header}\n\n\n{detector_body}\n\n\n{setup_class}\n"
    strat_path.write_text(merged, encoding="utf-8")
    return True


def move_shared_helpers() -> None:
    for name in ("_common.py", "_roadmap.py"):
        src = DET / name
        dst = STRAT / name
        if not src.is_file():
            continue
        text = fix_detector_imports(src.read_text(encoding="utf-8"))
        dst.write_text(text, encoding="utf-8")


def patch_spec_runtime() -> None:
    path = ROOT / "bot" / "setups" / "spec_runtime.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from .detectors import build_spec_signal\nfrom .detectors._common import SpecHit",
        "from ..strategies._common import SpecHit, build_spec_signal",
    )
    text = text.replace(
        "from .detectors._roadmap import _configured_params",
        "from ..strategies._roadmap import _configured_params",
    )
    text = text.replace(
        '"""Thin strategy shell — full detection in ``setups/detectors/*_setup``."""',
        '"""Thin strategy shell — detection in ``bot/strategies/*`` module."""',
    )
    text = text.replace(
        '"""Catalog-aligned setup detection — orchestration lives here, logic in detectors/."""',
        '"""Catalog-aligned setup detection — orchestration; logic in bot/strategies/."""',
    )
    path.write_text(text, encoding="utf-8")


def patch_roadmap_base() -> None:
    path = STRAT / "roadmap_base.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from ..setups.detectors._roadmap import (",
        "from ._roadmap import (",
    )
    text = text.replace(
        "detect logic in setups/detectors/.",
        "detect logic in bot/strategies/.",
    )
    path.write_text(text, encoding="utf-8")


def patch_spec_patterns() -> None:
    path = STRAT / "spec_patterns.py"
    path.write_text(
        '"""Re-export spec detector primitives from canonical ``bot.strategies._common``."""\n'
        "from __future__ import annotations\n\n"
        "from ._common import *  # noqa: F403\n",
        encoding="utf-8",
    )


def remove_detectors_dir() -> int:
    count = 0
    if not DET.is_dir():
        return 0
    for p in DET.iterdir():
        if p.is_file():
            count += 1
    shutil.rmtree(DET)
    return count


def main() -> None:
    move_shared_helpers()
    merged = 0
    for path in sorted(STRAT.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if path.name in ("roadmap_base.py", "catalog_spec.py", "spec_patterns.py", "common.py"):
            continue
        if consolidate_strategy(path.name):
            merged += 1
            print(f"merged {path.name}")
    patch_spec_runtime()
    patch_roadmap_base()
    patch_spec_patterns()
    deleted = remove_detectors_dir()
    print(f"merged_strategies={merged} deleted_detector_files={deleted}")


if __name__ == "__main__":
    main()
