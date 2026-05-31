"""Migrate remaining strategy detect bodies into detectors."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRAT = ROOT / "bot" / "strategies"
DET = ROOT / "bot" / "setups" / "detectors"

JOBS: list[tuple[str, str, str, str]] = [
    ("funding_reversal.py", "FundingReversalSetup", "_detect", "detect_funding_reversal"),
    ("supertrend_follow.py", "SuperTrendFollowSetup", "detect", "detect_supertrend_follow"),
    ("session_killzone.py", "SessionKillzoneSetup", "_detect", "detect_session_killzone"),
    ("rsi_divergence_bottom.py", "RSIDivergenceBottomSetup", "detect", "detect_rsi_divergence_bottom"),
    ("absorption.py", "AbsorptionSetup", "detect", "detect_absorption_prepared"),
    ("atr_expansion.py", "ATRExpansionSetup", "detect", "detect_atr_expansion_prepared"),
    ("aggression_shift.py", "AggressionShiftSetup", "detect", "detect_aggression_shift_prepared"),
    ("bb_squeeze.py", "BBSqueezeSetup", "detect", "detect_bb_squeeze_prepared"),
    ("wyckoff_spring.py", "WyckoffSpringSetup", "detect", "detect_wyckoff_spring_prepared"),
    ("stop_hunt_detection.py", "StopHuntDetectionSetup", "detect", "detect_stop_hunt_prepared"),
]


def _extract_method(source: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.get_source_segment(source, item) or ""
    raise ValueError(f"{method_name} not found in {class_name}")


def _extract_module_helpers(source: str, before_class: str) -> str:
    idx = source.index(f"class {before_class}")
    head = source[:idx]
    # keep only defs starting with def _ 
    lines = []
    for chunk in head.split("\n\n"):
        if chunk.strip().startswith("def _") or chunk.strip().startswith("_DEFAULT"):
            lines.append(chunk)
    return "\n\n".join(lines).strip()


def _to_detector_func(method_src: str, func_name: str, *, extra_params: str = "") -> str:
    lines = method_src.splitlines()[1:]
    out = []
    for line in lines:
        if line.startswith("        "):
            out.append(line[4:])
        else:
            out.append(line)
    body = "\n".join(out)
    body = body.replace("self.setup_id", "setup_id")
    body = body.replace("self.family", "family")
    body = body.replace("setup_id = self.setup_id", "pass  # setup_id passed")
    body = body.replace("self.get_optimizable_params(settings)", "get_optimizable_params(settings)")
    body = body.replace("self._params(prepared, settings)", "effective_params")
    body = body.replace("self._current_expansion_candidate", "_current_expansion_candidate")
    body = body.replace("self.active_session_name", "active_session_name")
    body = body.replace("self.is_active_now", "is_active_now")
    sig = (
        f"def {func_name}(\n"
        "    prepared: PreparedSymbol,\n"
        "    settings: BotSettings,\n"
        "    effective_params: dict[str, float],\n"
        "    *,\n"
        "    setup_id: str,\n"
        "    family: str,\n"
        f"{extra_params}"
        ") -> Signal | None:\n"
    )
    return sig + body


def _thin_strategy(src: str, class_name: str, mod: str, func_name: str) -> str:
    m = re.search(rf"class {class_name}\(.*?\):(.*?)(?=\n    def )", src, re.S)
    attrs = m.group(1) if m else ""
    extra = ""
    if "get_optimizable_params" in src:
        extra_methods = ""
        for name in ("get_optimizable_params", "active_session_name", "is_active_now"):
            try:
                seg = _extract_method(src, class_name, name)
            except ValueError:
                continue
            extra_methods += "\n" + seg
        extra = extra_methods
    return (
        "from __future__ import annotations\n\n"
        "from ..domain.config import BotSettings\n"
        "from ..domain.schemas import PreparedSymbol, Signal\n"
        "from ..setups.base import BaseSetup\n"
        f"from ..setups.detectors.{mod} import {func_name}\n\n\n"
        f"class {class_name}(BaseSetup):"
        + attrs
        + extra
        + f"""
    def detect(self, prepared: PreparedSymbol, settings: BotSettings):
        return {func_name}(
            prepared,
            settings,
            self._effective_params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )

    def _effective_params(self, prepared: PreparedSymbol, settings: BotSettings) -> dict[str, float]:
        from ..setups.utils import get_dynamic_params
        defaults = self.get_optimizable_params(settings)
        return {{**defaults, **get_dynamic_params(prepared, self.setup_id)}}


__all__ = ["{class_name}"]
"""
    )


def main() -> None:
    for strat_file, cls, method, func in JOBS:
        src = (STRAT / strat_file).read_text(encoding="utf-8")
        mod = strat_file.replace(".py", "")
        if mod == "bb_squeeze":
            target = DET / "bb_squeeze.py"
            # append prepared detector at end
            existing = target.read_text(encoding="utf-8")
            method_src = _extract_method(src, cls, method)
            func_body = _to_detector_func(method_src, func)
            if func not in existing:
                target.write_text(existing + "\n\n" + func_body + "\n", encoding="utf-8")
            (STRAT / strat_file).write_text(_thin_strategy(src, cls, "bb_squeeze", func), encoding="utf-8")
            print("appended", func, "to bb_squeeze.py")
            continue

        helpers = ""
        if mod == "session_killzone":
            helpers = _extract_module_helpers(src, cls) + "\n\n"

        method_src = _extract_method(src, cls, method)
        # figure imports from original file
        imp = set(re.findall(r"\b(_\w+|build_spec_signal|detect_\w+|get_dynamic_params|StrategyDecision|LOG)\b", method_src + helpers))
        header = f'"""{mod} detector."""\nfrom __future__ import annotations\n\n'
        if mod == "session_killzone":
            header += (
                "import logging\nimport math\nfrom datetime import datetime, timezone\n"
                "from typing import Any, cast\n\n"
            )
        header += "from ...domain.config import BotSettings\n"
        if "StrategyDecision" in imp:
            header += "from ...domain.strategies import StrategyDecision\n"
        header += "from ...domain.schemas import PreparedSymbol, Signal\n"
        if mod == "funding_reversal":
            header += "import logging\nimport math\n\n"
            header += "from ...features import _swing_points\n"
            header += "from .. import _build_signal, _compute_dynamic_score, _reject\n"
            header += "from ..utils import get_dynamic_params\n\nLOG = logging.getLogger(__name__)\n\n"
        elif mod == "supertrend_follow":
            header += "import math\n\n"
            header += "from ...features import _swing_points\n"
            header += "from .. import _build_signal, _compute_dynamic_score, _reject\n"
            header += "from ..utils import build_structural_targets, get_dynamic_params\n\n"
        elif mod in {"absorption", "atr_expansion", "aggression_shift", "wyckoff_spring", "stop_hunt_detection"}:
            header += "from ._roadmap import (\n"
            header += "    _build_atr_signal,\n    _flow_delta_with_source,\n    _last,\n    _missing_columns,\n    _reject,\n    _as_float,\n)\n"
            if "build_spec_signal" in method_src or "detect_" in method_src:
                header += "from .absorption import detect_absorption\n" if mod == "absorption" else ""
                if mod == "absorption":
                    header += "from ._common import build_spec_signal\n"
                if mod == "aggression_shift":
                    header += "from .aggression_shift import detect_aggression_shift\nfrom ._common import build_spec_signal\n"
                if mod == "wyckoff_spring":
                    header += "from .wyckoff_spring import detect_wyckoff_spring\nfrom ._common import build_spec_signal\n"
                if mod == "stop_hunt_detection":
                    header += "from .stop_hunt import detect_stop_hunt\nfrom ._common import build_spec_signal\n"
            header += "\n"
        elif mod == "rsi_divergence_bottom":
            header += "from ._roadmap import _as_float, _build_atr_signal, _missing_columns, _reject\n"
            header += "from ._common import build_spec_signal\n"
            header += "from .indicator_divergence import detect_regular_divergence\n\n"
        elif mod == "session_killzone":
            header += "from .. import _build_signal, _compute_dynamic_score, _reject\n"
            header += "from ..utils import get_dynamic_params\n\n"
            header += "LOG = logging.getLogger(__name__)\n\n"

        func_body = _to_detector_func(method_src, func)
        if mod == "session_killzone":
            func_body = func_body.replace(
                "return StrategyDecision.skip(",
                "from ...domain.strategies import StrategyDecision\n        return StrategyDecision.skip(",
                1,
            )
        out = header + helpers + f'__all__ = ["{func}"]\n\n\n' + func_body + "\n"
        (DET / f"{mod}.py").write_text(out, encoding="utf-8")

        if cls.endswith("Setup") and "RoadmapSetup" in src:
            thin = (
                "from __future__ import annotations\n\n"
                "from ..domain.config import BotSettings\n"
                "from ..domain.schemas import PreparedSymbol, Signal\n"
                "from .roadmap_base import RoadmapSetup\n"
                f"from ..setups.detectors.{mod} import {func}\n\n"
            )
            m = re.search(rf"class {cls}\(RoadmapSetup\):(.*?)(?=\n    def )", src, re.S)
            thin += f"class {cls}(RoadmapSetup):" + (m.group(1) if m else "") + f"""
    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return {func}(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["{cls}"]
"""
            (STRAT / strat_file).write_text(thin, encoding="utf-8")
        else:
            # keep get_optimizable_params in thin strategy - simplified
            (STRAT / strat_file).write_text(_thin_strategy(src, cls, mod, func), encoding="utf-8")
        print("migrated", mod)


if __name__ == "__main__":
    main()
